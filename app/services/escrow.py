# app/services/escrow.py
#
# Core escrow business logic for Zuno.
# This file contains functions that handle the actual
# movement of money — the most critical code on the platform.
#
# Every function here is called from route handlers.
# Routes handle HTTP. This file handles money.

from app import mysql
from app.utils.helpers import log_transaction_action
from datetime import datetime


def release_funds_to_seller(transaction_id, triggered_by='SYSTEM'):
    """
    Releases escrowed funds to the seller.

    Called when:
    - Buyer approves during inspection window
    - Inspection window expires (auto-complete)
    - Admin resolves dispute in seller's favor

    Creates a PENDING payout record.
    The scheduler picks it up and sends the actual
    M-Pesa B2C payment every 30 minutes.

    Args:
        transaction_id : the Zuno transaction ID
        triggered_by   : who triggered this release

    Returns:
        (True, message)  if successful
        (False, message) if failed
    """
    cursor = mysql.connection.cursor()

    # Get full transaction details
    cursor.execute(
        "SELECT * FROM transactions WHERE id = %s",
        (transaction_id,)
    )
    txn = cursor.fetchone()

    if not txn:
        cursor.close()
        return False, "Transaction not found"

    # Only release if in a valid state
    releasable_states = [
        'DELIVERED', 'INSPECTION', 'SHIPPED', 'DISPUTED'
    ]
    if txn['status'] not in releasable_states:
        cursor.close()
        return False, (
            f"Cannot release funds. "
            f"Status is '{txn['status']}'"
        )

    # Get seller phone for payout
    cursor.execute(
        "SELECT phone_number FROM users WHERE id = %s",
        (txn['seller_id'],)
    )
    seller = cursor.fetchone()

    if not seller:
        cursor.close()
        return False, "Seller not found"

    now = datetime.now()

    # Create payout record
    cursor.execute("""
        INSERT INTO payouts
            (seller_id, transaction_id, amount,
             mpesa_number, status)
        VALUES (%s, %s, %s, %s, 'PENDING')
    """, (
        txn['seller_id'],
        transaction_id,
        txn['seller_receives'],
        seller['phone_number']
    ))

    # Mark transaction as COMPLETED
    cursor.execute("""
        UPDATE transactions SET
            status = 'COMPLETED',
            completed_at = %s
        WHERE id = %s
    """, (now, transaction_id))

    mysql.connection.commit()
    cursor.close()

    log_transaction_action(
        transaction_id,
        action='FUNDS_RELEASED',
        performed_by=triggered_by,
        old_value=txn['status'],
        new_value='COMPLETED',
        details=(
            f"Payout of KES {txn['seller_receives']} "
            f"queued for seller"
        )
    )

    # Update seller reputation
    from app.services.trust_score import update_reputation_metrics
    try:
        update_reputation_metrics(txn['seller_id'])
    except Exception as e:
        print(f"[ESCROW] Reputation update failed: {str(e)}")

    return True, "Funds released successfully"


def refund_buyer(transaction_id, triggered_by='SYSTEM'):
    """
    Refunds the buyer after a dispute or cancellation.

    Buyer gets back what they paid including their
    share of the platform fee.

    Args:
        transaction_id : the Zuno transaction ID
        triggered_by   : who triggered this refund

    Returns:
        (True, message)  if successful
        (False, message) if failed
    """
    cursor = mysql.connection.cursor()

    cursor.execute(
        "SELECT * FROM transactions WHERE id = %s",
        (transaction_id,)
    )
    txn = cursor.fetchone()

    if not txn:
        cursor.close()
        return False, "Transaction not found"

    # Buyer gets back what they paid
    # (item amount + their fee share)
    refund_amount = (
        float(txn['amount']) + float(txn['buyer_fee_share'])
    )

    now = datetime.now()

    # Create refund payout to buyer
    cursor.execute("""
        INSERT INTO payouts
            (seller_id, transaction_id, amount,
             mpesa_number, status)
        VALUES (%s, %s, %s, %s, 'PENDING')
    """, (
        txn['seller_id'],    # still linked to seller for records
        transaction_id,
        refund_amount,
        txn['buyer_phone']   # but sent to buyer's phone
    ))

    # Mark transaction as REFUNDED
    cursor.execute("""
        UPDATE transactions SET
            status = 'REFUNDED',
            completed_at = %s
        WHERE id = %s
    """, (now, transaction_id))

    mysql.connection.commit()
    cursor.close()

    log_transaction_action(
        transaction_id,
        action='BUYER_REFUNDED',
        performed_by=triggered_by,
        old_value=txn['status'],
        new_value='REFUNDED',
        details=(
            f"Refund of KES {refund_amount} "
            f"queued for buyer {txn['buyer_phone']}"
        )
    )

    # Update seller reputation — dispute counts against them
    from app.services.trust_score import update_reputation_metrics
    try:
        update_reputation_metrics(txn['seller_id'])
    except Exception as e:
        print(f"[ESCROW] Reputation update failed: {str(e)}")

    return True, f"Refund of KES {refund_amount} initiated"