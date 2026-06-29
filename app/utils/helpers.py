# app/utils/helpers.py
#
# Shared utility functions used across the entire Zuno backend.
# The principle here is DRY — Don't Repeat Yourself.
# Instead of writing the same logic in 10 different places,
# we write it once here and import it wherever needed.

import random
import string
import os
from datetime import datetime
from app import mysql


def generate_transaction_id():
    """
    Generates a unique Zuno transaction ID.

    Format: ZNO-YYYYMMDD-XXXXX
    Example: ZNO-20260601-K3P9A

    Why this format?
    - ZNO prefix identifies it as a Zuno transaction
    - Date part makes it human-readable and sortable
    - Random 5-char suffix prevents collisions
    - Easy to read aloud over the phone for support calls
    """
    date_part = datetime.now().strftime('%Y%m%d')
    random_part = ''.join(
        random.choices(string.ascii_uppercase + string.digits, k=5)
    )
    return f"ZNO-{date_part}-{random_part}"


def calculate_fees(amount):
    """
    Calculates the complete financial breakdown for a Zuno transaction.

    Zuno charges a platform fee that is split equally between
    the buyer and the seller. Neither party bears the full cost.

    Example — Ksh 3,000 item:
        Platform fee (2.5%)  = Ksh 75.00
        Buyer pays half fee  = Ksh 37.50  → Total buyer pays: Ksh 3,037.50
        Seller pays half fee = Ksh 37.50  → Total seller gets: Ksh 2,962.50
        Zuno earns           = Ksh 75.00

    This fee-sharing model is a key business differentiator —
    most escrow platforms charge the full fee to one party.

    Returns a dictionary with every number needed for
    display, storage, and M-Pesa transactions.
    """
    fee_percent = float(os.getenv('PLATFORM_FEE_PERCENT', 2.5))
    buyer_share_percent = float(os.getenv('BUYER_FEE_SHARE_PERCENT', 50))

    amount = float(amount)

    # Total fee charged on this transaction
    total_fee = round((fee_percent / 100) * amount, 2)

    # Buyer's portion of the fee (added to what they pay)
    buyer_fee = round((buyer_share_percent / 100) * total_fee, 2)

    # Seller's portion of the fee (deducted from what they receive)
    seller_fee = round(total_fee - buyer_fee, 2)

    return {
        'amount': amount,                           # base item price
        'total_fee': total_fee,                     # total Zuno fee
        'buyer_fee_share': buyer_fee,               # buyer's fee contribution
        'seller_fee_share': seller_fee,             # seller's fee contribution
        'buyer_pays': round(amount + buyer_fee, 2), # what buyer sends to escrow
        'seller_receives': round(amount - seller_fee, 2)  # what seller gets out
    }


def log_transaction_action(transaction_id, action, performed_by,
                            old_value=None, new_value=None,
                            details=None, ip_address=None):
    """
    Records every action taken on every transaction to the audit log.

    WHY THIS MATTERS:
    In a financial platform, you need a complete record of everything
    that happened and when. If a seller claims "I never got paid" or
    a buyer says "I never confirmed delivery", this log is your proof.

    This is also required for regulatory compliance — financial
    regulators require transaction audit trails.

    Call this function EVERY time a transaction status changes.
    No exceptions. Every. Single. Time.

    Args:
        transaction_id : which transaction this is about
        action         : what happened  e.g. "STATUS_CHANGED"
        performed_by   : who did it     e.g. "0712345678" or "SYSTEM"
        old_value      : what it was before  e.g. "FUNDED"
        new_value      : what it is now      e.g. "SHIPPED"
        details        : any extra context
        ip_address     : where the request came from
    """
    cursor = mysql.connection.cursor()
    cursor.execute("""
        INSERT INTO transaction_logs
            (transaction_id, action, performed_by,
             old_value, new_value, details, ip_address)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (transaction_id, action, performed_by,
          old_value, new_value, details, ip_address))
    mysql.connection.commit()
    cursor.close()


def format_phone_for_mpesa(phone):
    """
    Converts any Kenyan phone number format to 254XXXXXXXXX
    which is the format Safaricom's M-Pesa API requires.

    Handles all common formats Kenyan users type:
        0712345678    → 254712345678
        +254712345678 → 254712345678
        254712345678  → 254712345678  (already correct)

    Returns None if the format is completely unrecognizable.
    """
    if not phone:
        return None

    phone = str(phone).strip().replace(' ', '').replace('-', '')

    if phone.startswith('+254'):
        return phone[1:]          # remove the + sign
    elif phone.startswith('0'):
        return '254' + phone[1:]  # replace leading 0 with 254
    elif phone.startswith('254') and len(phone) == 12:
        return phone              # already in correct format

    return None  # unrecognized — caller must handle this


def log_security_event(event_type, details,
                        ip_address=None, user_phone=None):
    """
    Records suspicious or security-relevant activity.

    Used for:
    - Failed payment link signature verifications (possible fraud)
    - Rate limit violations (possible bot/attack)
    - Access attempts on non-existent transactions
    - Failed login attempts

    This table is reviewed regularly to detect fraud patterns.
    """
    cursor = mysql.connection.cursor()
    cursor.execute("""
        INSERT INTO security_logs
            (event_type, details, ip_address, user_phone)
        VALUES (%s, %s, %s, %s)
    """, (event_type, details, ip_address, user_phone))
    mysql.connection.commit()
    cursor.close()