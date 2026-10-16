# app/routes/disputes.py
#
# Dispute System for Zuno.
#
# A dispute can be raised by either the buyer OR the seller
# during the inspection window.
#
# Once raised:
# - Transaction status → DISPUTED
# - Funds are frozen — nobody can touch them
# - Both parties enter the negotiation panel
# - If unresolved → admin arbitration
#
# ENDPOINTS:
# POST /api/disputes/raise              → raise a dispute
# GET  /api/disputes/<id>              → get dispute details
# POST /api/disputes/<id>/evidence     → submit evidence
# GET  /api/disputes/<id>/evidence     → view all evidence
# POST /api/disputes/<id>/escalate     → escalate to admin
# GET  /api/disputes/my                → get my disputes

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app import mysql
from app.utils.helpers import log_transaction_action
from app.utils.validators import validate_required_fields
from app.services.sms import send_dispute_notification, send_sms
from datetime import datetime, timedelta

disputes_bp = Blueprint('disputes', __name__)

# How long each party has to submit evidence
EVIDENCE_WINDOW_HOURS = 48

# How long admin has to resolve after evidence window closes
RESOLUTION_WINDOW_HOURS = 72


# ══════════════════════════════════════════════════════════════
# ENDPOINT 1 — RAISE A DISPUTE
# ══════════════════════════════════════════════════════════════

@disputes_bp.route('/raise', methods=['POST'])
def raise_dispute():
    """
    POST /api/disputes/raise

    Either buyer or seller raises a dispute on a transaction.
    Can only be raised during INSPECTION window or when FUNDED.

    Once raised:
    - Transaction frozen at current status
    - Both parties notified via SMS
    - Negotiation panel opens automatically
    - 48 hour evidence submission window starts

    No login required for buyers.
    Sellers must be logged in OR provide their phone.

    Expected body:
    {
        "transaction_id": "ZNO-20260825-AXK3J",
        "raised_by_phone": "0798765432",
        "raised_by_role": "buyer",
        "category": "ITEM_NOT_AS_DESCRIBED",
        "description": "The shoes I received are size 41 not 42 as agreed in the contract.",
        "preferred_resolution": "FULL_REFUND"
    }
    """
    data = request.get_json()

    # Validate required fields
    valid, error = validate_required_fields(data, [
        'transaction_id', 'raised_by_phone',
        'raised_by_role', 'category', 'description'
    ])
    if not valid:
        return jsonify({'error': error}), 400

    # Validate role
    if data['raised_by_role'] not in ['buyer', 'seller']:
        return jsonify({
            'error': 'raised_by_role must be buyer or seller'
        }), 400

    # Validate category
    valid_categories = [
        'ITEM_NOT_RECEIVED', 'ITEM_NOT_AS_DESCRIBED',
        'ITEM_DAMAGED', 'WRONG_ITEM',
        'DELIVERY_ISSUE', 'CONTRACT_BREACH', 'OTHER'
    ]
    if data['category'] not in valid_categories:
        return jsonify({
            'error': f"Category must be one of: "
                     f"{', '.join(valid_categories)}"
        }), 400

    # Description must be detailed
    if len(data['description'].strip()) < 30:
        return jsonify({
            'error': 'Please describe the issue in more detail '
                     '(minimum 30 characters). Be specific about '
                     'what went wrong.'
        }), 400

    cursor = mysql.connection.cursor()

    # Get the transaction
    cursor.execute(
        "SELECT * FROM transactions WHERE id = %s",
        (data['transaction_id'],)
    )
    txn = cursor.fetchone()

    if not txn:
        cursor.close()
        return jsonify({'error': 'Transaction not found'}), 404

    # Disputes can only be raised on active transactions
    disputable_statuses = ['FUNDED', 'IN_TRANSIT',
                           'DELIVERED', 'INSPECTION']
    if txn['status'] not in disputable_statuses:
        cursor.close()
        return jsonify({
            'error': f"Cannot raise dispute. "
                     f"Transaction status is '{txn['status']}'. "
                     f"Disputes can only be raised on active "
                     f"transactions."
        }), 400

    # Verify the person raising the dispute is part of this transaction
    if data['raised_by_role'] == 'buyer':
        if txn['buyer_phone'] != data['raised_by_phone']:
            cursor.close()
            return jsonify({
                'error': 'Phone number does not match '
                         'the buyer on this transaction'
            }), 403
    else:
        # Seller verification
        cursor.execute(
            "SELECT phone_number FROM users WHERE id = %s",
            (txn['seller_id'],)
        )
        seller = cursor.fetchone()
        if not seller or seller['phone_number'] != data['raised_by_phone']:
            cursor.close()
            return jsonify({
                'error': 'Phone number does not match '
                         'the seller on this transaction'
            }), 403

    # Check no dispute already exists for this transaction
    cursor.execute(
        "SELECT id FROM disputes WHERE transaction_id = %s",
        (data['transaction_id'],)
    )
    if cursor.fetchone():
        cursor.close()
        return jsonify({
            'error': 'A dispute already exists for this transaction. '
                     'Check your existing dispute.'
        }), 409

    now = datetime.now()

    # Set deadlines
    evidence_deadline = now + timedelta(hours=EVIDENCE_WINDOW_HOURS)
    resolution_deadline = (
        evidence_deadline + timedelta(hours=RESOLUTION_WINDOW_HOURS)
    )

    # Create the dispute
    cursor.execute("""
        INSERT INTO disputes (
            transaction_id, raised_by_phone, raised_by_role,
            category, description, preferred_resolution,
            status, evidence_deadline, resolution_deadline
        ) VALUES (%s,%s,%s,%s,%s,%s,'OPEN',%s,%s)
    """, (
        data['transaction_id'],
        data['raised_by_phone'],
        data['raised_by_role'],
        data['category'],
        data['description'].strip(),
        data.get('preferred_resolution', 'FULL_REFUND'),
        evidence_deadline,
        resolution_deadline
    ))

    dispute_id = cursor.lastrowid

    # Freeze the transaction — change status to DISPUTED
    cursor.execute("""
        UPDATE transactions
        SET status = 'DISPUTED'
        WHERE id = %s
    """, (data['transaction_id'],))

    mysql.connection.commit()

    # Log the dispute
    log_transaction_action(
        data['transaction_id'],
        action='DISPUTE_RAISED',
        performed_by=data['raised_by_phone'],
        old_value=txn['status'],
        new_value='DISPUTED',
        details=f"Category: {data['category']}. "
                f"Dispute ID: {dispute_id}"
    )

    # Get seller phone for notification
    cursor.execute(
        "SELECT phone_number FROM users WHERE id = %s",
        (txn['seller_id'],)
    )
    seller = cursor.fetchone()
    cursor.close()

    # Notify both parties
    other_party_phone = (
        seller['phone_number']
        if data['raised_by_role'] == 'buyer'
        else txn['buyer_phone']
    )

    send_dispute_notification(
        data['raised_by_phone'],
        data['transaction_id'],
        data['raised_by_role']
    )
    send_dispute_notification(
        other_party_phone,
        data['transaction_id'],
        'seller' if data['raised_by_role'] == 'buyer' else 'buyer'
    )

    return jsonify({
        'message': 'Dispute raised successfully. '
                   'Both parties have been notified.',
        'dispute_id': dispute_id,
        'status': 'OPEN',
        'evidence_deadline': str(evidence_deadline),
        'resolution_deadline': str(resolution_deadline),
        'next_steps': {
            'negotiate': f"POST /api/negotiation/{dispute_id}/message",
            'submit_evidence': f"POST /api/disputes/{dispute_id}/evidence",
            'escalate': f"POST /api/disputes/{dispute_id}/escalate"
        }
    }), 201


# ══════════════════════════════════════════════════════════════
# ENDPOINT 2 — GET DISPUTE DETAILS
# ══════════════════════════════════════════════════════════════

@disputes_bp.route('/<int:dispute_id>', methods=['GET'])
def get_dispute(dispute_id):
    """
    GET /api/disputes/<dispute_id>

    Returns full dispute details including the transaction
    and both parties information.
    """
    cursor = mysql.connection.cursor()
    cursor.execute("""
        SELECT
            d.*,
            t.item_name, t.amount, t.buyer_phone,
            t.buyer_name, t.delivery_method,
            u.full_name as seller_name,
            u.business_name as seller_business,
            u.phone_number as seller_phone
        FROM disputes d
        JOIN transactions t ON t.id = d.transaction_id
        JOIN users u ON u.id = t.seller_id
        WHERE d.id = %s
    """, (dispute_id,))

    dispute = cursor.fetchone()
    cursor.close()

    if not dispute:
        return jsonify({'error': 'Dispute not found'}), 404

    # Convert timestamps
    for field in ['created_at', 'resolved_at',
                  'evidence_deadline', 'resolution_deadline']:
        if dispute.get(field):
            dispute[field] = str(dispute[field])

    return jsonify({'dispute': dispute}), 200


# ══════════════════════════════════════════════════════════════
# ENDPOINT 3 — SUBMIT EVIDENCE
# ══════════════════════════════════════════════════════════════

@disputes_bp.route('/<int:dispute_id>/evidence', methods=['POST'])
def submit_evidence(dispute_id):
    """
    POST /api/disputes/<dispute_id>/evidence

    Submit evidence for a dispute.
    Both buyer and seller can submit evidence.
    Evidence must be submitted before the deadline.

    For MVP we accept a URL to an already-uploaded file.
    In production this would handle direct file upload
    to Cloudinary.

    Expected body:
    {
        "submitted_by_phone": "0798765432",
        "submitted_by_role": "buyer",
        "evidence_type": "photo",
        "file_url": "https://cloudinary.com/...",
        "description": "Photo showing wrong shoe size received"
    }
    """
    data = request.get_json()

    valid, error = validate_required_fields(data, [
        'submitted_by_phone', 'submitted_by_role',
        'evidence_type', 'file_url'
    ])
    if not valid:
        return jsonify({'error': error}), 400

    if data['evidence_type'] not in [
        'photo', 'screenshot', 'document', 'tracking'
    ]:
        return jsonify({
            'error': 'evidence_type must be: photo, screenshot, '
                     'document, or tracking'
        }), 400

    cursor = mysql.connection.cursor()

    # Get dispute and check deadline
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
            'error': 'This dispute has already been resolved.'
        }), 400

    # Check evidence deadline has not passed
    if datetime.now() > dispute['evidence_deadline']:
        cursor.close()
        return jsonify({
            'error': 'Evidence submission window has closed. '
                     'The dispute is now under admin review.'
        }), 400

    # Save evidence
    cursor.execute("""
        INSERT INTO dispute_evidence (
            dispute_id, submitted_by_phone,
            submitted_by_role, evidence_type,
            file_url, description
        ) VALUES (%s,%s,%s,%s,%s,%s)
    """, (
        dispute_id,
        data['submitted_by_phone'],
        data['submitted_by_role'],
        data['evidence_type'],
        data['file_url'],
        data.get('description', '')
    ))

    # Move dispute to EVIDENCE_COLLECTION if still OPEN
    if dispute['status'] == 'OPEN':
        cursor.execute("""
            UPDATE disputes SET status = 'EVIDENCE_COLLECTION'
            WHERE id = %s
        """, (dispute_id,))

    mysql.connection.commit()
    cursor.close()

    return jsonify({
        'message': 'Evidence submitted successfully.',
        'dispute_id': dispute_id
    }), 201


# ══════════════════════════════════════════════════════════════
# ENDPOINT 4 — GET ALL EVIDENCE
# ══════════════════════════════════════════════════════════════

@disputes_bp.route('/<int:dispute_id>/evidence', methods=['GET'])
def get_evidence(dispute_id):
    """
    GET /api/disputes/<dispute_id>/evidence

    Returns all evidence submitted for a dispute.
    Organized by who submitted it.
    """
    cursor = mysql.connection.cursor()
    cursor.execute("""
        SELECT * FROM dispute_evidence
        WHERE dispute_id = %s
        ORDER BY created_at ASC
    """, (dispute_id,))

    evidence = cursor.fetchall()
    cursor.close()

    # Separate buyer and seller evidence
    buyer_evidence = [e for e in evidence
                      if e['submitted_by_role'] == 'buyer']
    seller_evidence = [e for e in evidence
                       if e['submitted_by_role'] == 'seller']

    for item in evidence:
        if item.get('created_at'):
            item['created_at'] = str(item['created_at'])

    return jsonify({
        'dispute_id': dispute_id,
        'buyer_evidence': buyer_evidence,
        'seller_evidence': seller_evidence,
        'total': len(evidence)
    }), 200


# ══════════════════════════════════════════════════════════════
# ENDPOINT 5 — ESCALATE TO ADMIN
# ══════════════════════════════════════════════════════════════

@disputes_bp.route('/<int:dispute_id>/escalate', methods=['POST'])
def escalate_dispute(dispute_id):
    """
    POST /api/disputes/<dispute_id>/escalate

    Escalates the dispute to admin arbitration when
    the parties cannot resolve it themselves through
    the negotiation panel.

    Either party can escalate after 24 hours of
    the dispute being open with no resolution.

    Expected body:
    {
        "escalated_by_phone": "0798765432",
        "reason": "Seller is not responding in the negotiation panel"
    }
    """
    data = request.get_json()

    valid, error = validate_required_fields(
        data, ['escalated_by_phone']
    )
    if not valid:
        return jsonify({'error': error}), 400

    cursor = mysql.connection.cursor()
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

    if dispute['status'] == 'ESCALATED':
        cursor.close()
        return jsonify({
            'error': 'This dispute is already escalated to admin.'
        }), 400

    # Update dispute status to ESCALATED
    cursor.execute("""
        UPDATE disputes SET status = 'ESCALATED'
        WHERE id = %s
    """, (dispute_id,))
    mysql.connection.commit()

    # Get transaction for notification
    cursor.execute(
        "SELECT buyer_phone, seller_id, item_name "
        "FROM transactions WHERE id = %s",
        (dispute['transaction_id'],)
    )
    txn = cursor.fetchone()

    cursor.execute(
        "SELECT phone_number FROM users WHERE id = %s",
        (txn['seller_id'],)
    )
    seller = cursor.fetchone()
    cursor.close()

    # Notify both parties
    for phone in [txn['buyer_phone'], seller['phone_number']]:
        send_sms(
            phone,
            f"Zuno: Your dispute for '{txn['item_name']}' "
            f"has been escalated to our arbitration team. "
            f"We will review all evidence and make a final "
            f"decision within 72 hours. "
            f"Dispute ID: {dispute_id}"
        )

    return jsonify({
        'message': 'Dispute escalated to admin arbitration. '
                   'Both parties notified. '
                   'Decision within 72 hours.',
        'dispute_id': dispute_id,
        'status': 'ESCALATED'
    }), 200


# ══════════════════════════════════════════════════════════════
# ENDPOINT 6 — GET MY DISPUTES
# ══════════════════════════════════════════════════════════════

@disputes_bp.route('/my', methods=['GET'])
def get_my_disputes():
    """
    GET /api/disputes/my?phone=0798765432&role=buyer

    Returns all disputes for a specific phone number.
    Works for both buyers and sellers.
    """
    phone = request.args.get('phone')
    role = request.args.get('role', 'buyer')

    if not phone:
        return jsonify({'error': 'Phone number required'}), 400

    cursor = mysql.connection.cursor()

    if role == 'buyer':
        cursor.execute("""
            SELECT d.*, t.item_name, t.amount
            FROM disputes d
            JOIN transactions t ON t.id = d.transaction_id
            WHERE d.raised_by_phone = %s
            OR t.buyer_phone = %s
            ORDER BY d.created_at DESC
        """, (phone, phone))
    else:
        cursor.execute("""
            SELECT d.*, t.item_name, t.amount
            FROM disputes d
            JOIN transactions t ON t.id = d.transaction_id
            JOIN users u ON u.id = t.seller_id
            WHERE u.phone_number = %s
            ORDER BY d.created_at DESC
        """, (phone,))

    disputes = cursor.fetchall()
    cursor.close()

    for dispute in disputes:
        for field in ['created_at', 'resolved_at',
                      'evidence_deadline', 'resolution_deadline']:
            if dispute.get(field):
                dispute[field] = str(dispute[field])

    return jsonify({
        'disputes': disputes,
        'total': len(disputes)
    }), 200