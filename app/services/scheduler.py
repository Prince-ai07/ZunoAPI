# app/services/scheduler.py
#
# Background jobs that run automatically while
# the Zuno server is running.
#
# JOBS:
# 1. Auto-complete transactions where inspection
#    window has expired — runs every hour
# 2. Expire contracts where seller did not respond
#    — runs every hour
# 3. Process pending payouts — runs every 30 minutes

from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime

def auto_complete_expired_inspections(app):
    """
    Finds all transactions in INSPECTION status where
    the inspection window has expired and auto-completes them.

    This protects sellers from buyers who receive items
    but never bother to confirm or dispute.

    The buyer had their chance to raise a dispute.
    Window expired = funds released to seller.
    """
    with app.app_context():
        from app import mysql
        from app.services.sms import send_sms

        cursor = mysql.connection.cursor()

        # Find expired inspection windows
        cursor.execute("""
            SELECT t.id, t.buyer_phone, t.item_name,
                   t.seller_id, t.seller_receives,
                   u.phone_number as seller_phone
            FROM transactions t
            JOIN users u ON u.id = t.seller_id
            WHERE t.status = 'INSPECTION'
            AND t.inspection_ends_at <= %s
        """, (datetime.now(),))

        expired = cursor.fetchall()

        for txn in expired:
            # Mark as completed
            cursor.execute("""
                UPDATE transactions SET
                    status = 'COMPLETED',
                    completed_at = %s
                WHERE id = %s
            """, (datetime.now(), txn['id']))

            # Create payout record
            cursor.execute("""
                INSERT INTO payouts
                    (seller_id, transaction_id,
                     amount, mpesa_number, status)
                VALUES (%s, %s, %s, %s, 'PENDING')
            """, (
                txn['seller_id'],
                txn['id'],
                txn['seller_receives'],
                txn['seller_phone']
            ))

            mysql.connection.commit()

            # Log the auto-completion
            from app.utils.helpers import log_transaction_action
            log_transaction_action(
                txn['id'],
                action='AUTO_COMPLETED',
                performed_by='SYSTEM',
                old_value='INSPECTION',
                new_value='COMPLETED',
                details='Inspection window expired. '
                        'Funds auto-released to seller.'
            )

            # Notify both parties
            send_sms(
                txn['buyer_phone'],
                f"Zuno: Your inspection window for "
                f"'{txn['item_name']}' has expired. "
                f"Funds have been released to the seller. "
                f"Transaction ID: {txn['id']}"
            )

            send_sms(
                txn['seller_phone'],
                f"Zuno: Inspection window expired for "
                f"'{txn['item_name']}'. "
                f"Ksh {txn['seller_receives']:,.0f} is being "
                f"released to your M-Pesa. "
                f"Transaction ID: {txn['id']}"
            )

            # Update reputation
            from app.services.trust_score import update_reputation_metrics
            update_reputation_metrics(txn['seller_id'])

            print(f"[SCHEDULER] Auto-completed transaction {txn['id']}")

        cursor.close()


def expire_old_contracts(app):
    """
    Marks contracts as EXPIRED where the seller
    did not respond within 48 hours.
    Notifies buyers so they know to try another seller.
    """
    with app.app_context():
        from app import mysql
        from app.services.sms import send_sms

        cursor = mysql.connection.cursor()

        cursor.execute("""
            SELECT id, buyer_phone, item_name
            FROM deal_contracts
            WHERE status = 'SELLER_REVIEWING'
            AND expires_at <= %s
        """, (datetime.now(),))

        expired = cursor.fetchall()

        for contract in expired:
            cursor.execute("""
                UPDATE deal_contracts
                SET status = 'EXPIRED'
                WHERE id = %s
            """, (contract['id'],))

            mysql.connection.commit()

            send_sms(
                contract['buyer_phone'],
                f"Zuno: Your deal request for "
                f"'{contract['item_name']}' expired because "
                f"the seller did not respond in time. "
                f"Please try a different seller. "
                f"Contract ID: {contract['id']}"
            )

            print(f"[SCHEDULER] Contract {contract['id']} expired")

        cursor.close()


def process_pending_payouts(app):
    """
    Processes pending payouts to sellers.
    Picks up payout records with status PENDING
    and sends the actual M-Pesa B2C payment.
    """
    with app.app_context():
        from app import mysql
        from app.services.mpesa import send_b2c_payment

        cursor = mysql.connection.cursor()

        cursor.execute("""
            SELECT p.*, u.phone_number as seller_phone
            FROM payouts p
            JOIN users u ON u.id = p.seller_id
            WHERE p.status = 'PENDING'
            LIMIT 10
        """)

        pending = cursor.fetchall()

        for payout in pending:
            # Mark as processing first to avoid double-sending
            cursor.execute("""
                UPDATE payouts SET status = 'PROCESSING'
                WHERE id = %s
            """, (payout['id'],))
            mysql.connection.commit()

            # Send B2C payment
            result = send_b2c_payment(
                phone_number=payout['seller_phone'],
                amount=payout['amount'],
                transaction_id=payout['transaction_id'],
                remarks=f"Zuno payout for {payout['transaction_id']}"
            )

            if result['success']:
                # Save conversation ID for callback matching
                cursor.execute("""
                    UPDATE payouts SET
                        mpesa_reference = %s
                    WHERE id = %s
                """, (
                    result.get('conversation_id', ''),
                    payout['id']
                ))
                mysql.connection.commit()
                print(f"[PAYOUT] Sent Ksh {payout['amount']} "
                      f"to {payout['seller_phone']}")
            else:
                # Mark as failed
                cursor.execute("""
                    UPDATE payouts SET
                        status = 'FAILED',
                        failure_reason = %s
                    WHERE id = %s
                """, (result['message'], payout['id']))
                mysql.connection.commit()
                print(f"[PAYOUT FAILED] {result['message']}")

        cursor.close()


def start_scheduler(app):
    """
    Starts all background jobs.
    Called once when the Flask app starts.
    """
    scheduler = BackgroundScheduler()

    # Auto-complete expired inspection windows — every hour
    scheduler.add_job(
        func=auto_complete_expired_inspections,
        args=[app],
        trigger='interval',
        hours=1,
        id='auto_complete_inspections',
        replace_existing=True
    )

    # Expire old contracts — every hour
    scheduler.add_job(
        func=expire_old_contracts,
        args=[app],
        trigger='interval',
        hours=1,
        id='expire_contracts',
        replace_existing=True
    )

    # Process pending payouts — every 30 minutes
    scheduler.add_job(
        func=process_pending_payouts,
        args=[app],
        trigger='interval',
        minutes=30,
        id='process_payouts',
        replace_existing=True
    )

    scheduler.start()
    print("[SCHEDULER] Background jobs started successfully")
    print("[SCHEDULER] Jobs: auto-complete, expire contracts, payouts")