# app/routes/contracts.py
#
# Deal Contracts — the formal agreement between buyer and seller
# before any money moves on Zuno.
#
# FLOW:
# 1. Buyer creates contract → states expectations + delivery method
# 2. Seller gets SMS notification
# 3. Seller reviews → confirms or rejects
# 4. If confirmed → transaction can be created and funded
#
# ENDPOINTS:
# POST /api/contracts/create              → buyer creates contract
# GET  /api/contracts/<id>               → view contract details
# POST /api/contracts/<id>/confirm       → seller confirms
# POST /api/contracts/<id>/reject        → seller rejects
# GET  /api/contracts/my                 → seller sees all their contracts

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app import mysql
from app.utils.validators import (
    validate_required_fields,
    validate_phone,
    validate_amount,
    validate_delivery_method
)
from app.services.sms import send_sms
from datetime import datetime, timedelta
import os

contracts_bp = Blueprint('contracts', __name__)

# How many hours the seller has to respond to a contract
CONTRACT_EXPIRY_HOURS = int(os.getenv('CONTRACT_EXPIRY_HOURS', 48))


# ══════════════════════════════════════════════════════════════
# ENDPOINT 1 — CREATE CONTRACT
# ══════════════════════════════════════════════════════════════

@contracts_bp.route('/create', methods=['POST'])
def create_contract():
    """
    POST /api/contracts/create

    Buyer initiates a deal by creating a contract.
    No login required — buyers do not need a Zuno account.
    They just need the seller's ID from the seller's profile link.

    What happens:
    1. Buyer fills in item details and their expectations
    2. Buyer selects delivery method
    3. Contract is saved with status SELLER_REVIEWING
    4. Seller receives SMS notification
    5. Seller has 48 hours to respond

    Expected body:
    {
        "seller_id": 1,
        "buyer_phone": "0798765432",
        "buyer_name": "John Kamau",
        "item_name": "Nike Air Max Size 42",
        "item_description": "As shown in TikTok video",
        "agreed_amount": 3000,
        "buyer_expectations": "Size 42, black/white, brand new with box and receipt",
        "delivery_method": "PLATFORM_COURIER",
        "pickup_location": "Westlands Nairobi",
        "delivery_location": "Kilimani Nairobi"
    }
    """
    data = request.get_json()

    # ── Validate required fields ──────────────────────────────
    valid, error = validate_required_fields(data, [
        'seller_id', 'buyer_phone', 'buyer_name',
        'item_name', 'agreed_amount',
        'buyer_expectations', 'delivery_method'
    ])
    if not valid:
        return jsonify({'error': error}), 400

    # ── Validate phone number ─────────────────────────────────
    valid, error = validate_phone(data['buyer_phone'])
    if not valid:
        return jsonify({'error': error}), 400

    # ── Validate amount ───────────────────────────────────────
    valid, error = validate_amount(data['agreed_amount'])
    if not valid:
        return jsonify({'error': error}), 400

    # ── Validate delivery method ──────────────────────────────
    valid, error = validate_delivery_method(data['delivery_method'])
    if not valid:
        return jsonify({'error': error}), 400

    # ── Expectations must be detailed enough ──────────────────
    # We require at least 20 characters to force buyers
    # to be specific — vague expectations cause disputes
    if len(data['buyer_expectations'].strip()) < 20:
        return jsonify({
            'error': 'Please describe your expectations in more '
                     'detail — minimum 20 characters. Be specific '
                     'about size, color, condition, accessories etc.'
        }), 400

    # ── Verify seller exists and is active ────────────────────
    cursor = mysql.connection.cursor()
    cursor.execute("""
        SELECT id, full_name, business_name, phone_number,
               is_active, seller_tier
        FROM users
        WHERE id = %s
    """, (data['seller_id'],))

    seller = cursor.fetchone()

    if not seller:
        cursor.close()
        return jsonify({'error': 'Seller not found'}), 404

    if not seller['is_active']:
        cursor.close()
        return jsonify({
            'error': 'This seller account is currently unavailable'
        }), 400

    # ── Check seller tier transaction limit ───────────────────
    # Tier 0 sellers cannot accept deals above Ksh 1,000
    # This limits fraud exposure from new unverified accounts
    tier_limits = {0: 1000, 1: 10000, 2: 1000000}
    max_amount = tier_limits.get(seller['seller_tier'], 1000)

    if float(data['agreed_amount']) > max_amount:
        cursor.close()
        return jsonify({
            'error': f"This seller's current verification tier allows "
                     f"a maximum deal of Ksh {max_amount:,}. "
                     f"The seller must complete verification to "
                     f"accept higher value deals."
        }), 400

    # ── Set contract expiry ───────────────────────────────────
    # Seller must respond within 48 hours or contract expires
    expires_at = datetime.now() + timedelta(hours=CONTRACT_EXPIRY_HOURS)

    # ── Save contract to database ─────────────────────────────
    cursor.execute("""
        INSERT INTO deal_contracts (
            seller_id, buyer_phone, buyer_name,
            item_name, item_description, agreed_amount,
            buyer_expectations, delivery_method,
            pickup_location, delivery_location,
            status, expires_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'SELLER_REVIEWING',%s)
    """, (
        data['seller_id'],
        data['buyer_phone'],
        data['buyer_name'].strip(),
        data['item_name'].strip(),
        data.get('item_description', '').strip(),
        float(data['agreed_amount']),
        data['buyer_expectations'].strip(),
        data['delivery_method'],
        data.get('pickup_location', ''),
        data.get('delivery_location', ''),
        expires_at
    ))

    contract_id = cursor.lastrowid
    mysql.connection.commit()
    cursor.close()

    # ── Notify seller via SMS ─────────────────────────────────
    seller_name = seller['business_name'] or seller['full_name']
    send_sms(
        seller['phone_number'],
        f"Zuno: New deal request from {data['buyer_name']} "
        f"for '{data['item_name']}' at Ksh {float(data['agreed_amount']):,.0f}. "
        f"Contract ID: {contract_id}. "
        f"Log in to Zuno to review and respond. "
        f"You have 48 hours."
    )

    return jsonify({
        'message': 'Contract sent to seller. '
                   'They have 48 hours to confirm.',
        'contract_id': contract_id,
        'expires_at': str(expires_at),
        'next_step': 'Wait for seller confirmation before payment.'
    }), 201


# ══════════════════════════════════════════════════════════════
# ENDPOINT 2 — GET CONTRACT DETAILS
# ══════════════════════════════════════════════════════════════

@contracts_bp.route('/<int:contract_id>', methods=['GET'])
def get_contract(contract_id):
    """
    GET /api/contracts/<contract_id>

    Returns full contract details.
    Used by the seller to review the buyer's expectations
    before deciding to confirm or reject.
    Also used by the buyer to check the status.
    No login required — both parties access by contract ID.
    """
    cursor = mysql.connection.cursor()
    cursor.execute("""
        SELECT
            c.*,
            u.full_name as seller_full_name,
            u.business_name,
            u.is_verified,
            u.seller_tier
        FROM deal_contracts c
        JOIN users u ON u.id = c.seller_id
        WHERE c.id = %s
    """, (contract_id,))

    contract = cursor.fetchone()
    cursor.close()

    if not contract:
        return jsonify({'error': 'Contract not found'}), 404

    # Convert timestamps to strings for JSON
    for field in ['created_at', 'updated_at', 'expires_at',
                  'seller_confirmed_at']:
        if contract.get(field):
            contract[field] = str(contract[field])

    return jsonify({'contract': contract}), 200


# ══════════════════════════════════════════════════════════════
# ENDPOINT 3 — SELLER CONFIRMS CONTRACT
# ══════════════════════════════════════════════════════════════

@contracts_bp.route('/<int:contract_id>/confirm', methods=['POST'])
@jwt_required()
def confirm_contract(contract_id):
    """
    POST /api/contracts/<contract_id>/confirm

    Seller confirms they can meet the buyer's expectations.
    This is the seller's digital agreement to the deal terms.

    After this:
    - Contract status → AGREED
    - Buyer receives SMS notification
    - Buyer can now proceed to create and fund the transaction

    Requires seller to be logged in.

    Expected body:
    {
        "seller_confirmation": "Confirmed. I have the exact item,
         size 42 black/white Nike Air Max, brand new with box
         and original receipt as shown in my video."
    }
    """
    user_id = get_jwt_identity()
    data = request.get_json()

    # Seller must provide a confirmation statement
    if not data or not data.get('seller_confirmation', '').strip():
        return jsonify({
            'error': 'Please provide your confirmation statement '
                     'describing exactly what you will deliver.'
        }), 400

    cursor = mysql.connection.cursor()

    # Verify this contract belongs to the logged-in seller
    cursor.execute("""
        SELECT * FROM deal_contracts
        WHERE id = %s AND seller_id = %s
    """, (contract_id, user_id))

    contract = cursor.fetchone()

    if not contract:
        cursor.close()
        return jsonify({'error': 'Contract not found'}), 404

    # Can only confirm a contract that is awaiting review
    if contract['status'] != 'SELLER_REVIEWING':
        cursor.close()
        return jsonify({
            'error': f"Cannot confirm a contract with "
                     f"status '{contract['status']}'"
        }), 400

    # Check contract has not expired
    if datetime.now() > contract['expires_at']:
        cursor.execute(
            "UPDATE deal_contracts SET status='EXPIRED' WHERE id=%s",
            (contract_id,)
        )
        mysql.connection.commit()
        cursor.close()
        return jsonify({
            'error': 'This contract has expired. '
                     'The buyer must create a new one.'
        }), 400

    # Confirm the contract
    cursor.execute("""
        UPDATE deal_contracts SET
            status = 'AGREED',
            seller_confirmation = %s,
            seller_confirmed_at = %s,
            delivery_method_agreed = TRUE
        WHERE id = %s
    """, (
        data['seller_confirmation'].strip(),
        datetime.now(),
        contract_id
    ))
    mysql.connection.commit()
    cursor.close()

    # Notify buyer that seller has confirmed
    send_sms(
        contract['buyer_phone'],
        f"Zuno: Great news! The seller confirmed your deal for "
        f"'{contract['item_name']}' at Ksh {float(contract['agreed_amount']):,.0f}. "
        f"You can now proceed to payment. "
        f"Contract ID: {contract_id}"
    )

    return jsonify({
        'message': 'Contract confirmed successfully. '
                   'Buyer has been notified to proceed with payment.',
        'contract_id': contract_id,
        'status': 'AGREED'
    }), 200


# ══════════════════════════════════════════════════════════════
# ENDPOINT 4 — SELLER REJECTS CONTRACT
# ══════════════════════════════════════════════════════════════

@contracts_bp.route('/<int:contract_id>/reject', methods=['POST'])
@jwt_required()
def reject_contract(contract_id):
    """
    POST /api/contracts/<contract_id>/reject

    Seller rejects the contract because they cannot meet
    the buyer's expectations. No money ever moved so
    there is nothing to refund.

    Expected body:
    {
        "reason": "I only have size 41 available, not size 42."
    }
    """
    user_id = get_jwt_identity()
    data = request.get_json()

    cursor = mysql.connection.cursor()
    cursor.execute("""
        SELECT * FROM deal_contracts
        WHERE id = %s AND seller_id = %s
    """, (contract_id, user_id))

    contract = cursor.fetchone()

    if not contract:
        cursor.close()
        return jsonify({'error': 'Contract not found'}), 404

    if contract['status'] != 'SELLER_REVIEWING':
        cursor.close()
        return jsonify({
            'error': f"Cannot reject a contract with "
                     f"status '{contract['status']}'"
        }), 400

    reason = data.get('reason', 'Seller cannot meet requirements').strip()

    cursor.execute("""
        UPDATE deal_contracts SET
            status = 'REJECTED',
            seller_confirmation = %s
        WHERE id = %s
    """, (reason, contract_id))
    mysql.connection.commit()
    cursor.close()

    # Notify buyer of rejection
    send_sms(
        contract['buyer_phone'],
        f"Zuno: The seller has declined your deal request for "
        f"'{contract['item_name']}'. "
        f"Reason: {reason}. "
        f"No money was taken. You can contact another seller."
    )

    return jsonify({
        'message': 'Contract rejected. Buyer has been notified.',
        'contract_id': contract_id,
        'status': 'REJECTED'
    }), 200


# ══════════════════════════════════════════════════════════════
# ENDPOINT 5 — GET MY CONTRACTS (Seller)
# ══════════════════════════════════════════════════════════════

@contracts_bp.route('/my', methods=['GET'])
@jwt_required()
def get_my_contracts():
    """
    GET /api/contracts/my
    GET /api/contracts/my?status=SELLER_REVIEWING

    Returns all contracts for the logged-in seller.
    Optional filter by status via query parameter.

    A seller sees all incoming deal requests here —
    like an inbox of deal proposals from buyers.
    """
    user_id = get_jwt_identity()

    # Optional status filter from URL query parameter
    # e.g. /api/contracts/my?status=SELLER_REVIEWING
    status_filter = request.args.get('status')

    cursor = mysql.connection.cursor()

    if status_filter:
        cursor.execute("""
            SELECT * FROM deal_contracts
            WHERE seller_id = %s AND status = %s
            ORDER BY created_at DESC
        """, (user_id, status_filter.upper()))
    else:
        cursor.execute("""
            SELECT * FROM deal_contracts
            WHERE seller_id = %s
            ORDER BY created_at DESC
        """, (user_id,))

    contracts = cursor.fetchall()
    cursor.close()

    # Convert timestamps to strings for JSON
    for contract in contracts:
        for field in ['created_at', 'updated_at',
                      'expires_at', 'seller_confirmed_at']:
            if contract.get(field):
                contract[field] = str(contract[field])

    return jsonify({
        'contracts': contracts,
        'total': len(contracts)
    }), 200