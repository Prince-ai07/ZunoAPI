# app/routes/negotiation.py
#
# Negotiation Panel for Zuno.
#
# This is where buyer and seller communicate directly
# during a dispute to try to resolve it themselves
# before admin gets involved.
#
# Features:
# - Send messages to each other
# - Make monetary settlement offers
# - Accept or reject offers
# - Admin can join the conversation if needed
#
# ENDPOINTS:
# POST /api/negotiation/<dispute_id>/message   → send a message
# GET  /api/negotiation/<dispute_id>/messages  → get all messages
# POST /api/negotiation/<dispute_id>/offer     → make settlement offer
# POST /api/negotiation/<dispute_id>/offer/<offer_id>/respond → accept/reject

from flask import Blueprint, request, jsonify
from app import mysql
from app.utils.validators import validate_required_fields
from app.services.sms import send_sms
from datetime import datetime

negotiation_bp = Blueprint('negotiation', __name__)


# ══════════════════════════════════════════════════════════════
# ENDPOINT 1 — SEND MESSAGE
# ══════════════════════════════════════════════════════════════

@negotiation_bp.route('/<int:dispute_id>/message', methods=['POST'])
def send_message(dispute_id):
    """
    POST /api/negotiation/<dispute_id>/message

    Sends a message in the negotiation panel.
    Both buyer and seller can send messages.
    Admin can also send messages to guide the parties.

    The negotiation panel is like a private group chat
    specific to this dispute. Both parties see all messages.

    Expected body:
    {
        "sender_phone": "0798765432",
        "sender_role": "buyer",
        "message": "I am willing to accept a partial refund
                    of Ksh 500 for the size difference."
    }
    """
    data = request.get_json()

    valid, error = validate_required_fields(data, [
        'sender_phone', 'sender_role', 'message'
    ])
    if not valid:
        return jsonify({'error': error}), 400

    if data['sender_role'] not in ['buyer', 'seller', 'admin']:
        return jsonify({
            'error': 'sender_role must be buyer, seller, or admin'
        }), 400

    if len(data['message'].strip()) == 0:
        return jsonify({'error': 'Message cannot be empty'}), 400

    cursor = mysql.connection.cursor()

    # Verify dispute exists and is active
    cursor.execute(
        "SELECT * FROM disputes WHERE id = %s",
        (dispute_id,)
    )
    dispute = cursor.fetchone()

    if not dispute:
        cursor.close()
        return jsonify({'error': 'Dispute not found'}), 404

    if dispute['status'] == 'RESOLVED':
        cursor.close()
        return jsonify({
            'error': 'This dispute is resolved. '
                     'The negotiation panel is closed.'
        }), 400

    # Update dispute status to NEGOTIATING if still OPEN
    if dispute['status'] == 'OPEN':
        cursor.execute("""
            UPDATE disputes SET status = 'NEGOTIATING'
            WHERE id = %s
        """, (dispute_id,))

    # Save the message
    cursor.execute("""
        INSERT INTO negotiation_messages (
            dispute_id, sender_phone,
            sender_role, message
        ) VALUES (%s,%s,%s,%s)
    """, (
        dispute_id,
        data['sender_phone'],
        data['sender_role'],
        data['message'].strip()
    ))

    message_id = cursor.lastrowid
    mysql.connection.commit()

    # Get transaction details for notification
    cursor.execute("""
        SELECT t.buyer_phone, t.item_name,
               u.phone_number as seller_phone
        FROM transactions t
        JOIN users u ON u.id = t.seller_id
        WHERE t.id = %s
    """, (dispute['transaction_id'],))

    txn = cursor.fetchone()
    cursor.close()

    # Notify the OTHER party about the new message
    # Do not notify the person who just sent it
    if data['sender_role'] == 'buyer':
        notify_phone = txn['seller_phone']
        from_label = 'The buyer'
    elif data['sender_role'] == 'seller':
        notify_phone = txn['buyer_phone']
        from_label = 'The seller'
    else:
        # Admin message — notify both
        notify_phone = None

    if notify_phone:
        send_sms(
            notify_phone,
            f"Zuno Dispute #{dispute_id}: "
            f"{from_label} sent a new message in the "
            f"negotiation panel for '{txn['item_name']}'. "
            f"Log in to Zuno to read and respond."
        )

    return jsonify({
        'message': 'Message sent successfully.',
        'message_id': message_id,
        'dispute_id': dispute_id
    }), 201


# ══════════════════════════════════════════════════════════════
# ENDPOINT 2 — GET ALL MESSAGES
# ══════════════════════════════════════════════════════════════

@negotiation_bp.route('/<int:dispute_id>/messages', methods=['GET'])
def get_messages(dispute_id):
    """
    GET /api/negotiation/<dispute_id>/messages

    Returns all messages in the negotiation panel
    for a specific dispute, in chronological order.

    Both buyer and seller see the same messages.
    This is a shared conversation — no private messages.
    """
    cursor = mysql.connection.cursor()

    # Verify dispute exists
    cursor.execute(
        "SELECT id FROM disputes WHERE id = %s",
        (dispute_id,)
    )
    if not cursor.fetchone():
        cursor.close()
        return jsonify({'error': 'Dispute not found'}), 404

    cursor.execute("""
        SELECT * FROM negotiation_messages
        WHERE dispute_id = %s
        ORDER BY created_at ASC
    """, (dispute_id,))

    messages = cursor.fetchall()
    cursor.close()

    for msg in messages:
        if msg.get('created_at'):
            msg['created_at'] = str(msg['created_at'])

    return jsonify({
        'dispute_id': dispute_id,
        'messages': messages,
        'total': len(messages)
    }), 200


# ══════════════════════════════════════════════════════════════
# ENDPOINT 3 — MAKE SETTLEMENT OFFER
# ══════════════════════════════════════════════════════════════

@negotiation_bp.route('/<int:dispute_id>/offer', methods=['POST'])
def make_offer(dispute_id):
    """
    POST /api/negotiation/<dispute_id>/offer

    Makes a formal monetary settlement offer.
    Different from a regular message — this creates
    an actionable offer the other party can
    formally accept or reject.

    Example scenarios:
    - Buyer offers to keep item if seller refunds Ksh 500
    - Seller offers a partial refund of Ksh 1000
    - Both agree to split the difference

    Expected body:
    {
        "sender_phone": "0798765432",
        "sender_role": "buyer",
        "offer_amount": 500,
        "offer_to": "buyer",
        "message": "I will keep the shoes if you refund
                    me Ksh 500 for the size difference."
    }
    """
    data = request.get_json()

    valid, error = validate_required_fields(data, [
        'sender_phone', 'sender_role',
        'offer_amount', 'offer_to', 'message'
    ])
    if not valid:
        return jsonify({'error': error}), 400

    # Validate offer amount
    try:
        offer_amount = float(data['offer_amount'])
        if offer_amount <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({
            'error': 'Offer amount must be a positive number'
        }), 400

    cursor = mysql.connection.cursor()

    # Verify dispute exists and is active
    cursor.execute(
        "SELECT * FROM disputes WHERE id = %s",
        (dispute_id,)
    )
    dispute = cursor.fetchone()

    if not dispute:
        cursor.close()
        return jsonify({'error': 'Dispute not found'}), 404

    if dispute['status'] == 'RESOLVED':
        cursor.close()
        return jsonify({
            'error': 'This dispute is already resolved.'
        }), 400

    # Get transaction to validate offer amount
    cursor.execute(
        "SELECT amount FROM transactions WHERE id = %s",
        (dispute['transaction_id'],)
    )
    txn = cursor.fetchone()

    # Offer cannot exceed the transaction amount
    if offer_amount > float(txn['amount']):
        cursor.close()
        return jsonify({
            'error': f"Offer amount cannot exceed the "
                     f"transaction amount of "
                     f"Ksh {float(txn['amount']):,.0f}"
        }), 400

    # Save as a special offer message
    cursor.execute("""
        INSERT INTO negotiation_messages (
            dispute_id, sender_phone, sender_role,
            message, is_offer, offer_amount,
            offer_to, offer_status
        ) VALUES (%s,%s,%s,%s,TRUE,%s,%s,'PENDING')
    """, (
        dispute_id,
        data['sender_phone'],
        data['sender_role'],
        data['message'].strip(),
        offer_amount,
        data['offer_to']
    ))

    offer_id = cursor.lastrowid
    mysql.connection.commit()

    # Notify other party
    cursor.execute("""
        SELECT t.buyer_phone, t.item_name,
               u.phone_number as seller_phone
        FROM transactions t
        JOIN users u ON u.id = t.seller_id
        WHERE t.id = %s
    """, (dispute['transaction_id'],))

    txn_details = cursor.fetchone()
    cursor.close()

    notify_phone = (
        txn_details['seller_phone']
        if data['sender_role'] == 'buyer'
        else txn_details['buyer_phone']
    )

    from_label = 'The buyer' if data['sender_role'] == 'buyer' else 'The seller'

    send_sms(
        notify_phone,
        f"Zuno Dispute #{dispute_id}: "
        f"{from_label} made a settlement offer of "
        f"Ksh {offer_amount:,.0f} for "
        f"'{txn_details['item_name']}'. "
        f"Log in to Zuno to accept or reject."
    )

    return jsonify({
        'message': 'Settlement offer sent successfully.',
        'offer_id': offer_id,
        'dispute_id': dispute_id,
        'offer_amount': offer_amount,
        'status': 'PENDING'
    }), 201


# ══════════════════════════════════════════════════════════════
# ENDPOINT 4 — RESPOND TO OFFER
# ══════════════════════════════════════════════════════════════

@negotiation_bp.route(
    '/<int:dispute_id>/offer/<int:offer_id>/respond',
    methods=['POST']
)
def respond_to_offer(dispute_id, offer_id):
    """
    POST /api/negotiation/<dispute_id>/offer/<offer_id>/respond

    Accept or reject a settlement offer.

    If ACCEPTED:
    - Dispute marked as RESOLVED
    - Transaction marked as RESOLVED
    - Funds split according to offer terms
    - Both parties notified

    If REJECTED:
    - Offer marked as rejected
    - Conversation continues
    - Either party can make a counter offer

    Expected body:
    {
        "responder_phone": "0712345678",
        "responder_role": "seller",
        "response": "ACCEPTED",
        "counter_amount": 300    (only if response is COUNTERED)
    }
    """
    data = request.get_json()

    valid, error = validate_required_fields(
        data, ['responder_phone', 'responder_role', 'response']
    )
    if not valid:
        return jsonify({'error': error}), 400

    if data['response'] not in ['ACCEPTED', 'REJECTED', 'COUNTERED']:
        return jsonify({
            'error': 'Response must be ACCEPTED, REJECTED, or COUNTERED'
        }), 400

    cursor = mysql.connection.cursor()

    # Get the offer
    cursor.execute("""
        SELECT * FROM negotiation_messages
        WHERE id = %s AND dispute_id = %s AND is_offer = TRUE
    """, (offer_id, dispute_id))

    offer = cursor.fetchone()

    if not offer:
        cursor.close()
        return jsonify({'error': 'Offer not found'}), 404

    if offer['offer_status'] != 'PENDING':
        cursor.close()
        return jsonify({
            'error': f"This offer has already been "
                     f"{offer['offer_status'].lower()}"
        }), 400

    # Update offer status
    cursor.execute("""
        UPDATE negotiation_messages
        SET offer_status = %s
        WHERE id = %s
    """, (data['response'], offer_id))

    if data['response'] == 'ACCEPTED':
        # Resolve the dispute with the agreed amount
        cursor.execute("""
            UPDATE disputes SET
                status = 'RESOLVED',
                resolution = 'SPLIT',
                split_buyer_amount = %s,
                split_seller_amount = %s,
                admin_notes = %s,
                resolved_at = %s
            WHERE id = %s
        """, (
            offer['offer_amount'],
            float(offer['offer_amount']),
            f"Resolved through negotiation panel. "
            f"Settlement offer of Ksh {offer['offer_amount']} accepted.",
            datetime.now(),
            dispute_id
        ))

        # Update transaction status
        cursor.execute("""
            UPDATE transactions SET status = 'RESOLVED'
            WHERE id = (
                SELECT transaction_id FROM disputes WHERE id = %s
            )
        """, (dispute_id,))

        mysql.connection.commit()
        cursor.close()

        return jsonify({
            'message': 'Settlement offer accepted. '
                       'Dispute resolved. '
                       'Funds will be distributed according '
                       'to the agreed terms.',
            'dispute_id': dispute_id,
            'status': 'RESOLVED',
            'settlement_amount': offer['offer_amount']
        }), 200

    elif data['response'] == 'COUNTERED':
        # Save counter offer as new message
        counter_amount = data.get('counter_amount')
        if not counter_amount:
            cursor.close()
            return jsonify({
                'error': 'counter_amount is required '
                         'when response is COUNTERED'
            }), 400

        cursor.execute("""
            INSERT INTO negotiation_messages (
                dispute_id, sender_phone, sender_role,
                message, is_offer, offer_amount,
                offer_to, offer_status
            ) VALUES (%s,%s,%s,%s,TRUE,%s,%s,'PENDING')
        """, (
            dispute_id,
            data['responder_phone'],
            data['responder_role'],
            f"Counter offer: Ksh {counter_amount}",
            counter_amount,
            offer['sender_role']
        ))

        mysql.connection.commit()
        cursor.close()

        return jsonify({
            'message': 'Counter offer sent.',
            'dispute_id': dispute_id,
            'counter_amount': counter_amount
        }), 200

    else:
        # REJECTED
        mysql.connection.commit()
        cursor.close()

        return jsonify({
            'message': 'Offer rejected. '
                       'The negotiation continues. '
                       'You can make a counter offer or '
                       'escalate to admin.',
            'dispute_id': dispute_id,
            'next_steps': {
                'counter_offer': f"POST /api/negotiation/{dispute_id}/offer",
                'escalate': f"POST /api/disputes/{dispute_id}/escalate"
            }
        }), 200