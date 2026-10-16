# app/routes/payments.py
#
# Payment endpoints for Zuno.
#
# ENDPOINTS:
# POST /api/payments/initiate          → buyer triggers STK Push
# POST /api/payments/mpesa/callback    → Safaricom calls this after payment
# POST /api/payments/mpesa/b2c/result  → Safaricom calls this after payout
# GET  /api/payments/status/<txn_id>   → check payment status
#
# THE CALLBACK URLS ARE THE MOST IMPORTANT ENDPOINTS ON THE PLATFORM.
# Safaricom calls them automatically — we never call them ourselves.
# They must be publicly accessible (not localhost) in production.
# For development testing we use ngrok to expose localhost.

from flask import Blueprint, request, jsonify
from app import mysql
from app.utils.helpers import log_transaction_action, log_security_event
from app.utils.validators import validate_phone, validate_required_fields
from app.services.mpesa import initiate_stk_push, send_b2c_payment
from app.services.sms import (
    send_deal_funded_notification,
    send_funds_released_notification
)
from datetime import datetime

payments_bp = Blueprint('payments', __name__)


# ══════════════════════════════════════════════════════════════
# ENDPOINT 1 — INITIATE PAYMENT (STK Push)
# ══════════════════════════════════════════════════════════════

@payments_bp.route('/initiate', methods=['POST'])
def initiate_payment():
    """
    POST /api/payments/initiate

    Buyer triggers M-Pesa payment for a transaction.
    Sends an STK Push to buyer's phone — they see a
    payment prompt and enter their PIN to pay.

    No login required — buyer uses their phone number.

    Expected body:
    {
        "transaction_id": "ZNO-20260601-A3K9P",
        "buyer_phone": "0712345678"
    }

    What happens:
    1. Validate transaction exists and is PENDING
    2. Verify buyer phone matches transaction
    3. Send STK Push via Daraja API
    4. Save checkout request ID for callback matching
    5. Return confirmation that push was sent
    """
    data = request.get_json()

    valid, error = validate_required_fields(
        data, ['transaction_id', 'buyer_phone']
    )
    if not valid:
        return jsonify({'error': error}), 400

    valid, error = validate_phone(data['buyer_phone'])
    if not valid:
        return jsonify({'error': error}), 400

    cursor = mysql.connection.cursor()

    # Get transaction details
    cursor.execute(
        "SELECT * FROM transactions WHERE id = %s",
        (data['transaction_id'],)
    )
    txn = cursor.fetchone()

    if not txn:
        cursor.close()
        return jsonify({'error': 'Transaction not found'}), 404

    # Verify buyer phone matches
    if txn['buyer_phone'] != data['buyer_phone']:
        cursor.close()
        return jsonify({
            'error': 'Phone number does not match this transaction'
        }), 403

    # Can only pay for PENDING transactions
    if txn['status'] != 'PENDING':
        cursor.close()
        return jsonify({
            'error': f"This transaction cannot be paid. "
                     f"Status is '{txn['status']}'"
        }), 400

    cursor.close()

    # Send STK Push to buyer's phone
    result = initiate_stk_push(
        phone_number=data['buyer_phone'],
        amount=txn['buyer_pays'],
        transaction_id=txn['id'],
        description=f"Zuno escrow payment"
    )

    if not result['success']:
        return jsonify({'error': result['message']}), 500

    # Save the checkout request ID so we can match
    # it when Safaricom sends the callback
    cursor = mysql.connection.cursor()
    cursor.execute("""
        UPDATE transactions
        SET mpesa_checkout_id = %s
        WHERE id = %s
    """, (result['checkout_request_id'], txn['id']))
    mysql.connection.commit()
    cursor.close()

    log_transaction_action(
        txn['id'],
        action='STK_PUSH_SENT',
        performed_by=data['buyer_phone'],
        details=f"Checkout ID: {result['checkout_request_id']}"
    )

    return jsonify({
        'message': result['message'],
        'transaction_id': txn['id'],
        'amount': txn['buyer_pays'],
        'instruction': 'Check your phone and enter your M-Pesa PIN '
                       'to complete payment.'
    }), 200


# ══════════════════════════════════════════════════════════════
# ENDPOINT 2 — M-PESA CALLBACK (Safaricom calls this)
# ══════════════════════════════════════════════════════════════

@payments_bp.route('/mpesa/callback', methods=['POST'])
def mpesa_callback():
    """
    POST /api/payments/mpesa/callback

    Safaricom calls this endpoint automatically after
    a buyer completes or cancels an STK Push payment.

    WE NEVER CALL THIS OURSELVES.
    Safaricom calls it. We just process what they send.

    This is the most critical endpoint on the platform.
    If this fails, payments are not recorded.

    SECURITY:
    In production, verify the request comes from
    Safaricom's IP range. For now we validate by
    matching the checkout request ID.

    IDEMPOTENCY:
    If Safaricom sends the same callback twice
    (it happens), we must not process it twice.
    We check if payment_reference already exists.
    """
    # Get the callback data Safaricom sent
    data = request.get_json()

    if not data:
        return jsonify({'ResultCode': 0, 'ResultDesc': 'Accepted'}), 200

    print(f"[MPESA CALLBACK] Received: {data}")

    # Safaricom wraps everything in 'Body' → 'stkCallback'
    try:
        stk_callback = data['Body']['stkCallback']
        result_code = stk_callback['ResultCode']
        checkout_request_id = stk_callback['CheckoutRequestID']
    except (KeyError, TypeError) as e:
        print(f"[MPESA CALLBACK] Invalid format: {e}")
        # Always return 200 to Safaricom even on error
        # Otherwise they keep retrying
        return jsonify({'ResultCode': 0, 'ResultDesc': 'Accepted'}), 200

    cursor = mysql.connection.cursor()

    # Find the transaction matching this checkout request
    cursor.execute("""
        SELECT * FROM transactions
        WHERE mpesa_checkout_id = %s
    """, (checkout_request_id,))

    txn = cursor.fetchone()

    if not txn:
        print(f"[MPESA CALLBACK] No transaction found for "
              f"checkout ID: {checkout_request_id}")
        cursor.close()
        return jsonify({'ResultCode': 0, 'ResultDesc': 'Accepted'}), 200

    # ── Payment SUCCESSFUL ────────────────────────────────────
    if result_code == 0:

        # Extract payment details from callback
        callback_metadata = stk_callback.get('CallbackMetadata', {})
        items = callback_metadata.get('Item', [])

        # Parse the metadata items into a dictionary
        # Safaricom sends: [{'Name': 'Amount', 'Value': 3037},
        #                   {'Name': 'MpesaReceiptNumber', 'Value': 'QHJ2K8LP3X'}...]
        metadata = {}
        for item in items:
            metadata[item['Name']] = item.get('Value')

        mpesa_ref = metadata.get('MpesaReceiptNumber', '')
        amount_paid = metadata.get('Amount', 0)

        print(f"[MPESA CALLBACK] Payment successful. "
              f"Ref: {mpesa_ref}, Amount: {amount_paid}")

        # ── Idempotency check ─────────────────────────────────
        # If this reference already exists, payment was
        # already processed — do not process again
        cursor.execute("""
            SELECT id FROM transactions
            WHERE payment_reference = %s
        """, (mpesa_ref,))

        if cursor.fetchone():
            print(f"[MPESA CALLBACK] Duplicate callback ignored "
                  f"for ref: {mpesa_ref}")
            cursor.close()
            return jsonify({
                'ResultCode': 0,
                'ResultDesc': 'Accepted'
            }), 200

        # ── Update transaction to FUNDED ──────────────────────
        now = datetime.now()
        cursor.execute("""
            UPDATE transactions SET
                status = 'FUNDED',
                payment_reference = %s,
                funded_at = %s
            WHERE id = %s
        """, (mpesa_ref, now, txn['id']))

        mysql.connection.commit()

        log_transaction_action(
            txn['id'],
            action='PAYMENT_RECEIVED',
            performed_by='MPESA_CALLBACK',
            old_value='PENDING',
            new_value='FUNDED',
            details=f"M-Pesa Ref: {mpesa_ref}, "
                    f"Amount: Ksh {amount_paid}"
        )

        # Get seller phone for notification
        cursor.execute(
            "SELECT phone_number FROM users WHERE id = %s",
            (txn['seller_id'],)
        )
        seller = cursor.fetchone()
        cursor.close()

        # Notify seller that payment is secured
        if seller:
            send_deal_funded_notification(
                seller['phone_number'],
                txn['item_name'],
                txn['seller_receives'],
                txn['id']
            )

        print(f"[MPESA CALLBACK] Transaction {txn['id']} "
              f"marked as FUNDED")

    # ── Payment FAILED or CANCELLED ───────────────────────────
    else:
        result_desc = stk_callback.get('ResultDesc', 'Payment failed')
        print(f"[MPESA CALLBACK] Payment failed: {result_desc}")

        log_transaction_action(
            txn['id'],
            action='PAYMENT_FAILED',
            performed_by='MPESA_CALLBACK',
            details=f"ResultCode: {result_code}, "
                    f"Reason: {result_desc}"
        )
        cursor.close()

    # Always return 200 to Safaricom
    # If we return any other code, they keep retrying
    return jsonify({'ResultCode': 0, 'ResultDesc': 'Accepted'}), 200


# ══════════════════════════════════════════════════════════════
# ENDPOINT 3 — B2C RESULT (Safaricom calls this after payout)
# ══════════════════════════════════════════════════════════════

@payments_bp.route('/mpesa/b2c/result', methods=['POST'])
def b2c_result():
    """
    POST /api/payments/mpesa/b2c/result

    Safaricom calls this after a B2C payout completes.
    We use it to update the payout record status and
    notify the seller their money has arrived.
    """
    data = request.get_json()

    if not data:
        return jsonify({'ResultCode': 0, 'ResultDesc': 'Accepted'}), 200

    print(f"[MPESA B2C RESULT] Received: {data}")

    try:
        result = data['Result']
        result_code = result['ResultCode']
        conversation_id = result['ConversationID']
    except (KeyError, TypeError) as e:
        print(f"[MPESA B2C] Invalid format: {e}")
        return jsonify({'ResultCode': 0, 'ResultDesc': 'Accepted'}), 200

    cursor = mysql.connection.cursor()

    # Find the payout matching this conversation
    # (We store conversation_id in mpesa_reference column)
    if result_code == 0:

        # Extract result parameters
        params = {}
        result_params = result.get('ResultParameters', {})
        for item in result_params.get('ResultParameter', []):
            params[item['Key']] = item.get('Value')

        mpesa_ref = params.get('TransactionReceipt', '')
        amount = params.get('TransactionAmount', 0)
        receiver_phone = params.get('ReceiverPartyPublicName', '')

        print(f"[MPESA B2C] Payout successful. "
              f"Ref: {mpesa_ref}, Amount: {amount}")

        # Update payout record
        cursor.execute("""
            UPDATE payouts SET
                status = 'COMPLETED',
                mpesa_reference = %s,
                completed_at = %s
            WHERE mpesa_reference = %s
               OR status = 'PROCESSING'
        """, (mpesa_ref, datetime.now(), conversation_id))

        mysql.connection.commit()
        cursor.close()

    else:
        # Payout failed — mark it so we can retry
        cursor.execute("""
            UPDATE payouts SET
                status = 'FAILED',
                failure_reason = %s
            WHERE mpesa_reference = %s
        """, (
            result.get('ResultDesc', 'Unknown error'),
            conversation_id
        ))
        mysql.connection.commit()
        cursor.close()

        print(f"[MPESA B2C] Payout failed: "
              f"{result.get('ResultDesc')}")

    return jsonify({'ResultCode': 0, 'ResultDesc': 'Accepted'}), 200


# ══════════════════════════════════════════════════════════════
# ENDPOINT 4 — CHECK PAYMENT STATUS
# ══════════════════════════════════════════════════════════════

@payments_bp.route('/status/<transaction_id>', methods=['GET'])
def check_payment_status(transaction_id):
    """
    GET /api/payments/status/<transaction_id>

    Returns the current payment status of a transaction.
    Used by the frontend to poll and update the UI
    after the buyer has initiated payment.

    Frontend polls this every 5 seconds until
    status changes from PENDING to FUNDED.
    """
    cursor = mysql.connection.cursor()
    cursor.execute("""
        SELECT id, status, payment_reference,
               funded_at, buyer_pays
        FROM transactions
        WHERE id = %s
    """, (transaction_id,))

    txn = cursor.fetchone()
    cursor.close()

    if not txn:
        return jsonify({'error': 'Transaction not found'}), 404

    if txn.get('funded_at'):
        txn['funded_at'] = str(txn['funded_at'])

    return jsonify({
        'transaction_id': txn['id'],
        'status': txn['status'],
        'payment_reference': txn['payment_reference'],
        'funded_at': txn.get('funded_at'),
        'is_paid': txn['status'] == 'FUNDED'
    }), 200