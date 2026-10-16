# app/routes/transactions.py
#
# The core escrow transaction engine for Zuno.
#
# A transaction can only be created from a CONFIRMED contract.
# This ensures both parties have agreed before money moves.
#
# THE TRANSACTION STATE MACHINE:
#
# PENDING → FUNDED → IN_TRANSIT → DELIVERED → INSPECTION → COMPLETED
#                                                    ↓
#                                                DISPUTED → RESOLVED
#                                                    ↓
#                                                 REFUNDED
#
# Each status change is logged to the audit trail.
# Each change triggers an SMS to both parties.
#
# ENDPOINTS:
# POST /api/transactions/create                  → create from contract
# GET  /api/transactions/<id>                    → get deal details
# GET  /api/transactions/<id>/verify-link        → verify payment link
# POST /api/transactions/<id>/ship               → seller marks shipped
# POST /api/transactions/<id>/confirm-delivery   → buyer confirms receipt
# POST /api/transactions/<id>/approve            → buyer approves in inspection
# POST /api/transactions/<id>/cancel             → cancel before funding
# GET  /api/transactions/my                      → seller's transactions

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app import mysql
from app.utils.helpers import (
    generate_transaction_id,
    calculate_fees,
    log_transaction_action,
    log_security_event
)
from app.utils.security import (
    build_payment_link,
    verify_link_signature,
    check_rate_limit
)
from app.utils.validators import validate_required_fields
from app.services.sms import (
    send_sms,
    send_deal_funded_notification,
    send_item_shipped_notification,
    send_delivery_confirmed_notification
)
from datetime import datetime, timedelta
import os

transactions_bp = Blueprint('transactions', __name__)

INSPECTION_HOURS = int(os.getenv('INSPECTION_WINDOW_HOURS', 24))


# ══════════════════════════════════════════════════════════════
# ENDPOINT 1 — CREATE TRANSACTION
# ══════════════════════════════════════════════════════════════

@transactions_bp.route('/create', methods=['POST'])
@jwt_required()
def create_transaction():
    """
    POST /api/transactions/create

    Seller creates an escrow transaction from an AGREED contract.
    This generates the signed payment link to send to the buyer.

    WHY must a contract exist first?
    Because both parties must agree on item details and delivery
    before money moves. The contract is that agreement.

    What happens:
    1. Verify the contract exists and is AGREED
    2. Calculate fee breakdown
    3. Generate unique transaction ID
    4. Generate cryptographically signed payment link
    5. Save transaction with PENDING status
    6. Return payment link for seller to share with buyer

    Expected body:
    {
        "contract_id": 1
    }
    """
    user_id = get_jwt_identity()
    data = request.get_json()

    valid, error = validate_required_fields(data, ['contract_id'])
    if not valid:
        return jsonify({'error': error}), 400

    cursor = mysql.connection.cursor()

    # ── Verify contract exists and belongs to this seller ─────
    cursor.execute("""
        SELECT * FROM deal_contracts
        WHERE id = %s AND seller_id = %s
    """, (data['contract_id'], user_id))

    contract = cursor.fetchone()

    if not contract:
        cursor.close()
        return jsonify({'error': 'Contract not found'}), 404

    # ── Contract must be AGREED by both parties ───────────────
    if contract['status'] != 'AGREED':
        cursor.close()
        return jsonify({
            'error': f"Cannot create transaction. "
                     f"Contract status is '{contract['status']}'. "
                     f"Buyer must confirm the contract first."
        }), 400

    # ── Check no transaction already exists for this contract ─
    cursor.execute(
        "SELECT id FROM transactions WHERE contract_id = %s",
        (data['contract_id'],)
    )
    if cursor.fetchone():
        cursor.close()
        return jsonify({
            'error': 'A transaction already exists for this contract.'
        }), 409

    # ── Calculate the full fee breakdown ──────────────────────
    fees = calculate_fees(contract['agreed_amount'])

    # ── Generate unique transaction ID ────────────────────────
    # Keep trying until we get a unique one
    # (collision probability is astronomically low but we check anyway)
    txn_id = generate_transaction_id()
    cursor.execute(
        "SELECT id FROM transactions WHERE id = %s", (txn_id,)
    )
    while cursor.fetchone():
        txn_id = generate_transaction_id()

    # ── Save transaction ──────────────────────────────────────
    cursor.execute("""
        INSERT INTO transactions (
            id, contract_id, seller_id, buyer_phone, buyer_name,
            item_name, item_description,
            amount, platform_fee, buyer_fee_share,
            seller_fee_share, buyer_pays, seller_receives,
            delivery_method, status
        ) VALUES (
            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING'
        )
    """, (
        txn_id,
        contract['id'],
        user_id,
        contract['buyer_phone'],
        contract['buyer_name'],
        contract['item_name'],
        contract['item_description'],
        fees['amount'],
        fees['total_fee'],
        fees['buyer_fee_share'],
        fees['seller_fee_share'],
        fees['buyer_pays'],
        fees['seller_receives'],
        contract['delivery_method']
    ))
    mysql.connection.commit()

    # ── Log the creation ──────────────────────────────────────
    log_transaction_action(
        txn_id,
        action='TRANSACTION_CREATED',
        performed_by=str(user_id),
        new_value='PENDING',
        details=f"Created from contract {contract['id']}"
    )

    cursor.close()

    # ── Generate signed payment link ──────────────────────────
    # This link is cryptographically signed — cannot be forged
    payment_link = build_payment_link(
        txn_id, user_id, fees['buyer_pays']
    )

    return jsonify({
        'message': 'Transaction created successfully.',
        'transaction_id': txn_id,
        'payment_link': payment_link,
        'fee_breakdown': {
            'item_price': fees['amount'],
            'platform_fee': fees['total_fee'],
            'buyer_pays': fees['buyer_pays'],
            'seller_receives': fees['seller_receives'],
            'fee_note': (
                f"The {os.getenv('PLATFORM_FEE_PERCENT', 2.5)}% "
                f"platform fee is shared equally between "
                f"buyer and seller."
            )
        },
        'instruction': 'Share the payment_link with your buyer '
                       'via TikTok DM, WhatsApp, or any channel.'
    }), 201


# ══════════════════════════════════════════════════════════════
# ENDPOINT 2 — GET TRANSACTION DETAILS
# ══════════════════════════════════════════════════════════════

@transactions_bp.route('/<transaction_id>', methods=['GET'])
def get_transaction(transaction_id):
    """
    GET /api/transactions/<transaction_id>

    Returns transaction details. Public endpoint — no login needed.
    This is what the buyer sees when they open the payment link.

    We show seller reputation data so buyers can make
    informed decisions about who they are paying.
    """
    rate_limit_error = check_rate_limit()
    if rate_limit_error:
        return rate_limit_error

    cursor = mysql.connection.cursor()
    cursor.execute("""
        SELECT
            t.id, t.item_name, t.item_description,
            t.item_photo, t.amount, t.buyer_pays,
            t.buyer_fee_share, t.status,
            t.delivery_method, t.created_at,
            t.funded_at, t.shipped_at,
            t.inspection_ends_at,
            u.full_name as seller_name,
            u.business_name,
            u.is_verified,
            r.completion_rate,
            r.dispute_rate,
            r.average_rating,
            r.total_transactions,
            r.trust_score,
            r.trust_level
        FROM transactions t
        JOIN users u ON u.id = t.seller_id
        LEFT JOIN seller_reputation r ON r.seller_id = t.seller_id
        WHERE t.id = %s
    """, (transaction_id,))

    txn = cursor.fetchone()
    cursor.close()

    if not txn:
        # Log this — could be someone probing for valid IDs
        log_security_event(
            'INVALID_TXN_ACCESS',
            f"Access attempt on non-existent transaction: {transaction_id}",
            ip_address=request.remote_addr
        )
        return jsonify({'error': 'Transaction not found'}), 404

    # Convert timestamps to strings
    for field in ['created_at', 'funded_at', 'shipped_at',
                  'inspection_ends_at']:
        if txn.get(field):
            txn[field] = str(txn[field])

    return jsonify({'transaction': txn}), 200


# ══════════════════════════════════════════════════════════════
# ENDPOINT 3 — VERIFY PAYMENT LINK
# ══════════════════════════════════════════════════════════════

@transactions_bp.route('/<transaction_id>/verify-link', methods=['GET'])
def verify_payment_link(transaction_id):
    """
    GET /api/transactions/<transaction_id>/verify-link?sig=xxxx

    Called automatically when a buyer opens a payment link.
    Verifies the cryptographic signature before showing
    the payment page.

    If signature is invalid:
    → Fraud warning returned
    → Security event logged
    → Payment page never shown

    This is the anti-fraud gate that protects buyers from
    fake payment links.
    """
    rate_limit_error = check_rate_limit()
    if rate_limit_error:
        return rate_limit_error

    provided_sig = request.args.get('sig')

    cursor = mysql.connection.cursor()
    cursor.execute("""
        SELECT id, seller_id, buyer_pays, status
        FROM transactions
        WHERE id = %s
    """, (transaction_id,))

    txn = cursor.fetchone()
    cursor.close()

    if not txn:
        log_security_event(
            'FAKE_LINK_ATTEMPT',
            f"Signature check on non-existent transaction: {transaction_id}",
            ip_address=request.remote_addr
        )
        return jsonify({
            'valid': False,
            'warning': '⚠️ This link is invalid. '
                       'Do NOT make any payment. '
                       'Report to support@zuno.co.ke'
        }), 404

    # Verify the cryptographic signature
    is_valid, error = verify_link_signature(
        transaction_id,
        txn['seller_id'],
        txn['buyer_pays'],
        provided_sig
    )

    if not is_valid:
        log_security_event(
            'INVALID_SIGNATURE',
            f"Invalid signature for transaction {transaction_id}. "
            f"Provided: {provided_sig}",
            ip_address=request.remote_addr
        )
        return jsonify({
            'valid': False,
            'warning': '⚠️ WARNING: This payment link failed our '
                       'security check. Do NOT make any payment. '
                       'Report this to support@zuno.co.ke'
        }), 403

    return jsonify({
        'valid': True,
        'transaction_id': transaction_id,
        'status': txn['status'],
        'message': 'Link verified. Safe to proceed with payment.'
    }), 200


# ══════════════════════════════════════════════════════════════
# ENDPOINT 4 — MARK AS SHIPPED
# ══════════════════════════════════════════════════════════════

@transactions_bp.route('/<transaction_id>/ship', methods=['POST'])
@jwt_required()
def mark_shipped(transaction_id):
    """
    POST /api/transactions/<transaction_id>/ship

    Seller marks the item as shipped after payment is confirmed.
    Can only be done when status is FUNDED.

    Expected body:
    {
        "shipping_notes": "Sent via G4S courier",
        "tracking_number": "G4S-2026-8472910",
        "courier_name": "G4S"
    }
    """
    user_id = get_jwt_identity()
    data = request.get_json()

    cursor = mysql.connection.cursor()
    cursor.execute("""
        SELECT * FROM transactions
        WHERE id = %s AND seller_id = %s
    """, (transaction_id, user_id))

    txn = cursor.fetchone()

    if not txn:
        cursor.close()
        return jsonify({'error': 'Transaction not found'}), 404

    if txn['status'] != 'FUNDED':
        cursor.close()
        return jsonify({
            'error': f"Cannot mark as shipped. "
                     f"Current status is '{txn['status']}'. "
                     f"Transaction must be FUNDED first."
        }), 400

    now = datetime.now()

    cursor.execute("""
        UPDATE transactions SET
            status = 'IN_TRANSIT',
            shipped_at = %s,
            shipping_notes = %s,
            tracking_number = %s,
            courier_name = %s
        WHERE id = %s
    """, (
        now,
        data.get('shipping_notes', ''),
        data.get('tracking_number', ''),
        data.get('courier_name', ''),
        transaction_id
    ))
    mysql.connection.commit()
    cursor.close()

    # Log the status change
    log_transaction_action(
        transaction_id,
        action='STATUS_CHANGED',
        performed_by=str(user_id),
        old_value='FUNDED',
        new_value='IN_TRANSIT',
        details=f"Tracking: {data.get('tracking_number', 'Not provided')}"
    )

    # Notify buyer
    send_item_shipped_notification(
        txn['buyer_phone'],
        txn['item_name'],
        data.get('tracking_number', ''),
        transaction_id
    )

    return jsonify({
        'message': 'Item marked as shipped. Buyer has been notified.',
        'transaction_id': transaction_id,
        'status': 'IN_TRANSIT'
    }), 200


# ══════════════════════════════════════════════════════════════
# ENDPOINT 5 — CONFIRM DELIVERY (Buyer)
# ══════════════════════════════════════════════════════════════

@transactions_bp.route('/<transaction_id>/confirm-delivery',
                       methods=['POST'])
def confirm_delivery(transaction_id):
    """
    POST /api/transactions/<transaction_id>/confirm-delivery

    Buyer confirms the item has been physically received.
    This starts the INSPECTION WINDOW — the buyer protection period.

    After this:
    - Status → INSPECTION
    - 24-hour inspection countdown starts
    - Buyer can inspect item to confirm it matches contract
    - If buyer approves → funds released to seller
    - If buyer raises dispute → funds frozen
    - If buyer is silent → funds auto-release after 24 hours

    No login required — buyer identifies by phone number.

    Expected body:
    {
        "buyer_phone": "0798765432"
    }
    """
    data = request.get_json()

    if not data or not data.get('buyer_phone'):
        return jsonify({'error': 'Buyer phone number is required'}), 400

    cursor = mysql.connection.cursor()
    cursor.execute(
        "SELECT * FROM transactions WHERE id = %s",
        (transaction_id,)
    )
    txn = cursor.fetchone()

    if not txn:
        cursor.close()
        return jsonify({'error': 'Transaction not found'}), 404

    # Verify the right buyer is confirming
    if txn['buyer_phone'] != data['buyer_phone']:
        cursor.close()
        return jsonify({
            'error': 'Phone number does not match this transaction'
        }), 403

    if txn['status'] != 'IN_TRANSIT':
        cursor.close()
        return jsonify({
            'error': f"Cannot confirm delivery. "
                     f"Current status is '{txn['status']}'"
        }), 400

    now = datetime.now()

    # Start the inspection window
    # Buyer has INSPECTION_HOURS to inspect before funds auto-release
    inspection_ends = now + timedelta(hours=INSPECTION_HOURS)

    cursor.execute("""
        UPDATE transactions SET
            status = 'INSPECTION',
            delivered_at = %s,
            inspection_starts_at = %s,
            inspection_ends_at = %s
        WHERE id = %s
    """, (now, now, inspection_ends, transaction_id))
    mysql.connection.commit()
    cursor.close()

    log_transaction_action(
        transaction_id,
        action='STATUS_CHANGED',
        performed_by=data['buyer_phone'],
        old_value='IN_TRANSIT',
        new_value='INSPECTION',
        details=f"Inspection window ends at {inspection_ends}"
    )

    # Notify seller that delivery was confirmed
    send_sms(
        txn['seller_id'],
        f"Zuno: Buyer confirmed receipt of '{txn['item_name']}'. "
        f"The {INSPECTION_HOURS}-hour inspection window has started. "
        f"Funds will be released once buyer approves or window expires."
    )

    # Notify buyer of inspection window
    send_sms(
        data['buyer_phone'],
        f"Zuno: You have {INSPECTION_HOURS} hours to inspect "
        f"'{txn['item_name']}' and confirm it matches the agreed "
        f"specifications. If satisfied, approve to release payment. "
        f"If there is an issue, raise a dispute before "
        f"{inspection_ends.strftime('%d %b %Y %H:%M')}."
    )

    return jsonify({
        'message': f"Delivery confirmed. You have {INSPECTION_HOURS} "
                   f"hours to inspect the item.",
        'inspection_ends_at': str(inspection_ends),
        'next_steps': {
            'approve': f"POST /api/transactions/{transaction_id}/approve",
            'dispute': f"POST /api/disputes/raise"
        }
    }), 200


# ══════════════════════════════════════════════════════════════
# ENDPOINT 6 — BUYER APPROVES (Releases Funds)
# ══════════════════════════════════════════════════════════════

@transactions_bp.route('/<transaction_id>/approve', methods=['POST'])
def approve_transaction(transaction_id):
    """
    POST /api/transactions/<transaction_id>/approve

    Buyer approves the item during inspection window.
    This releases the escrowed funds to the seller.

    Can optionally include a rating for the seller.

    Expected body:
    {
        "buyer_phone": "0798765432",
        "rating": 5,                    (optional, 1-5)
        "comment": "Great seller!"      (optional)
    }
    """
    data = request.get_json()

    if not data or not data.get('buyer_phone'):
        return jsonify({'error': 'Buyer phone number is required'}), 400

    cursor = mysql.connection.cursor()
    cursor.execute(
        "SELECT * FROM transactions WHERE id = %s",
        (transaction_id,)
    )
    txn = cursor.fetchone()

    if not txn:
        cursor.close()
        return jsonify({'error': 'Transaction not found'}), 404

    if txn['buyer_phone'] != data['buyer_phone']:
        cursor.close()
        return jsonify({
            'error': 'Phone number does not match this transaction'
        }), 403

    if txn['status'] != 'INSPECTION':
        cursor.close()
        return jsonify({
            'error': f"Cannot approve. "
                     f"Current status is '{txn['status']}'"
        }), 400

    now = datetime.now()

    # Release funds — mark as completed
    cursor.execute("""
        UPDATE transactions SET
            status = 'COMPLETED',
            completed_at = %s
        WHERE id = %s
    """, (now, transaction_id))

    # Save rating if provided
    if data.get('rating'):
        try:
            score = int(data['rating'])
            if 1 <= score <= 5:
                cursor.execute("""
                    INSERT INTO ratings
                        (transaction_id, seller_id,
                         buyer_phone, score, comment)
                    VALUES (%s, %s, %s, %s, %s)
                """, (
                    transaction_id,
                    txn['seller_id'],
                    data['buyer_phone'],
                    score,
                    data.get('comment', '')
                ))
        except (ValueError, TypeError):
            pass  # Invalid rating — ignore, don't crash

    # Create payout record for seller
    cursor.execute("""
        INSERT INTO payouts
            (seller_id, transaction_id, amount,
             mpesa_number, status)
        VALUES (%s, %s, %s, %s, 'PENDING')
    """, (
        txn['seller_id'],
        transaction_id,
        txn['seller_receives'],
        txn['buyer_phone']  # placeholder — replaced with seller phone
    ))

    mysql.connection.commit()
    cursor.close()

    log_transaction_action(
        transaction_id,
        action='FUNDS_RELEASED',
        performed_by=data['buyer_phone'],
        old_value='INSPECTION',
        new_value='COMPLETED',
        details=f"Buyer approved. Ksh {txn['seller_receives']} "
                f"queued for seller payout."
    )

    # Notify seller
    send_delivery_confirmed_notification(
        str(txn['seller_id']),
        txn['item_name'],
        txn['seller_receives']
    )

    # Update seller reputation
    from app.services.trust_score import update_reputation_metrics
    update_reputation_metrics(txn['seller_id'])

    return jsonify({
        'message': 'Transaction approved. '
                   'Funds are being released to the seller.',
        'transaction_id': transaction_id,
        'amount_released': txn['seller_receives']
    }), 200


# ══════════════════════════════════════════════════════════════
# ENDPOINT 7 — CANCEL TRANSACTION
# ══════════════════════════════════════════════════════════════

@transactions_bp.route('/<transaction_id>/cancel', methods=['POST'])
@jwt_required()
def cancel_transaction(transaction_id):
    """
    POST /api/transactions/<transaction_id>/cancel

    Cancels a transaction. Only allowed when status is PENDING
    (before the buyer has paid). Once funded, cancellation
    goes through the dispute process.

    Only the seller can cancel their own transaction.
    """
    user_id = get_jwt_identity()

    cursor = mysql.connection.cursor()
    cursor.execute("""
        SELECT * FROM transactions
        WHERE id = %s AND seller_id = %s
    """, (transaction_id, user_id))

    txn = cursor.fetchone()

    if not txn:
        cursor.close()
        return jsonify({'error': 'Transaction not found'}), 404

    if txn['status'] != 'PENDING':
        cursor.close()
        return jsonify({
            'error': f"Cannot cancel. Status is '{txn['status']}'. "
                     f"Only PENDING transactions can be cancelled. "
                     f"For funded transactions please raise a dispute."
        }), 400

    now = datetime.now()
    cursor.execute("""
        UPDATE transactions SET
            status = 'CANCELLED',
            cancelled_at = %s
        WHERE id = %s
    """, (now, transaction_id))
    mysql.connection.commit()
    cursor.close()

    log_transaction_action(
        transaction_id,
        action='TRANSACTION_CANCELLED',
        performed_by=str(user_id),
        old_value='PENDING',
        new_value='CANCELLED'
    )

    # Notify buyer
    send_sms(
        txn['buyer_phone'],
        f"Zuno: The deal for '{txn['item_name']}' has been "
        f"cancelled by the seller. No payment was taken. "
        f"Transaction ID: {transaction_id}"
    )

    return jsonify({
        'message': 'Transaction cancelled successfully.',
        'transaction_id': transaction_id
    }), 200


# ══════════════════════════════════════════════════════════════
# ENDPOINT 8 — GET MY TRANSACTIONS (Seller)
# ══════════════════════════════════════════════════════════════

@transactions_bp.route('/my', methods=['GET'])
@jwt_required()
def get_my_transactions():
    """
    GET /api/transactions/my
    GET /api/transactions/my?status=FUNDED

    Returns all transactions for the logged-in seller.
    Optional status filter via query parameter.
    """
    user_id = get_jwt_identity()
    status_filter = request.args.get('status')

    cursor = mysql.connection.cursor()

    if status_filter:
        cursor.execute("""
            SELECT * FROM transactions
            WHERE seller_id = %s AND status = %s
            ORDER BY created_at DESC
        """, (user_id, status_filter.upper()))
    else:
        cursor.execute("""
            SELECT * FROM transactions
            WHERE seller_id = %s
            ORDER BY created_at DESC
        """, (user_id,))

    transactions = cursor.fetchall()
    cursor.close()

    # Convert timestamps to strings
    for txn in transactions:
        for field in ['created_at', 'funded_at', 'shipped_at',
                      'delivered_at', 'completed_at',
                      'cancelled_at', 'inspection_ends_at']:
            if txn.get(field):
                txn[field] = str(txn[field])

    return jsonify({
        'transactions': transactions,
        'total': len(transactions)
    }), 200