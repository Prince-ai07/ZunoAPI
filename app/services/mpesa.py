# app/services/mpesa.py
#
# Safaricom Daraja API Integration for Zuno.
#
# This file handles ALL communication with Safaricom's M-Pesa API.
# It has two main functions:
#
# 1. STK PUSH — Send a payment request to buyer's phone
#    Buyer sees M-Pesa popup, enters PIN, pays.
#    Safaricom calls our callback URL to confirm.
#
# 2. B2C PAYMENT — Send money to seller's M-Pesa
#    Called after buyer approves the transaction.
#    Money goes directly to seller's M-Pesa number.
#
# HOW DARAJA AUTHENTICATION WORKS:
# Every API call needs a temporary access token.
# We get this token by sending our Consumer Key + Secret
# to Safaricom. The token expires after 1 hour.
# We request a fresh one for each payment.

import requests
import base64
import os
from datetime import datetime


def get_access_token():
    """
    Gets a temporary access token from Safaricom.

    Every M-Pesa API call requires this token.
    It works like a temporary password that expires
    after 1 hour.

    HOW IT WORKS:
    1. We combine Consumer Key and Consumer Secret
    2. We encode them in Base64 format
    3. We send them to Safaricom's OAuth endpoint
    4. Safaricom returns a token we use in API calls

    Returns the access token string or None if it fails.
    """
    consumer_key = os.getenv('MPESA_CONSUMER_KEY')
    consumer_secret = os.getenv('MPESA_CONSUMER_SECRET')
    environment = os.getenv('MPESA_ENVIRONMENT', 'sandbox')

    # Choose the right URL based on environment
    if environment == 'production':
        url = 'https://api.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials'
    else:
        url = 'https://sandbox.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials'

    # Encode credentials in Base64
    # This is the format Safaricom requires
    credentials = f"{consumer_key}:{consumer_secret}"
    encoded = base64.b64encode(credentials.encode()).decode()

    try:
        response = requests.get(
            url,
            headers={'Authorization': f'Basic {encoded}'}
        )
        result = response.json()
        token = result.get('access_token')

        if not token:
            print(f"[MPESA] Failed to get token: {result}")
            return None

        print(f"[MPESA] Access token obtained successfully")
        return token

    except Exception as e:
        print(f"[MPESA] Token error: {str(e)}")
        return None


def generate_password():
    """
    Generates the M-Pesa STK Push password.

    Safaricom requires a password built from:
    Shortcode + Passkey + Current Timestamp

    All combined and encoded in Base64.
    This password is only valid for the exact timestamp used.
    """
    shortcode = os.getenv('MPESA_SHORTCODE')
    passkey = os.getenv('MPESA_PASSKEY')

    # Timestamp format required by Safaricom: YYYYMMDDHHmmss
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')

    # Combine and encode
    raw = f"{shortcode}{passkey}{timestamp}"
    password = base64.b64encode(raw.encode()).decode()

    return password, timestamp


def initiate_stk_push(phone_number, amount, transaction_id, description):
    """
    Sends a payment request (STK Push) to the buyer's phone.

    STK Push = SIM Toolkit Push
    This makes a payment popup appear on the buyer's M-Pesa
    app asking them to enter their PIN to pay.

    FLOW:
    1. We call this function with buyer's phone and amount
    2. Safaricom sends a popup to buyer's phone
    3. Buyer enters their M-Pesa PIN
    4. Safaricom processes the payment
    5. Safaricom calls our callback URL with the result
    6. Our callback handler (in payments.py) processes the result

    Args:
        phone_number   : buyer's phone e.g. "0712345678"
        amount         : amount in KES e.g. 3037.50
        transaction_id : our Zuno transaction ID
        description    : what shows on buyer's M-Pesa prompt

    Returns dict with:
        success: True/False
        checkout_request_id: ID to track this payment
        message: description of what happened
    """
    # Get access token first
    token = get_access_token()
    if not token:
        return {
            'success': False,
            'message': 'Failed to connect to M-Pesa. Please try again.'
        }

    # Format phone number to 254XXXXXXXXX
    phone = _format_phone(phone_number)
    if not phone:
        return {
            'success': False,
            'message': 'Invalid phone number format.'
        }

    shortcode = os.getenv('MPESA_SHORTCODE')
    callback_url = os.getenv('MPESA_CALLBACK_URL')
    environment = os.getenv('MPESA_ENVIRONMENT', 'sandbox')
    password, timestamp = generate_password()

    # Amount must be a whole number for M-Pesa
    amount_int = int(round(float(amount)))

    # API endpoint
    if environment == 'production':
        url = 'https://api.safaricom.co.ke/mpesa/stkpush/v1/processrequest'
    else:
        url = 'https://sandbox.safaricom.co.ke/mpesa/stkpush/v1/processrequest'

    # Build the request payload
    payload = {
        # Business shortcode receiving the payment
        'BusinessShortCode': shortcode,

        # Generated password
        'Password': password,

        # Current timestamp
        'Timestamp': timestamp,

        # Always "CustomerPayBillOnline" for STK Push
        'TransactionType': 'CustomerPayBillOnline',

        # Amount buyer will pay
        'Amount': amount_int,

        # Buyer's phone number
        'PartyA': phone,

        # Your business shortcode (same as BusinessShortCode)
        'PartyB': shortcode,

        # Phone to receive confirmation SMS (buyer's phone)
        'PhoneNumber': phone,

        # Where Safaricom sends the payment result
        'CallBackURL': callback_url,

        # Reference shown on buyer's M-Pesa
        # Keep it short — max 12 characters
        'AccountReference': f"ZNO{transaction_id[-5:]}",

        # Description shown on buyer's phone
        'TransactionDesc': description[:20]
    }

    try:
        response = requests.post(
            url,
            json=payload,
            headers={
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json'
            }
        )

        result = response.json()
        print(f"[MPESA STK] Response: {result}")

        # ResponseCode "0" means the push was sent successfully
        # It does NOT mean the buyer has paid yet
        if result.get('ResponseCode') == '0':
            return {
                'success': True,
                'checkout_request_id': result.get('CheckoutRequestID'),
                'message': 'Payment request sent to your phone. '
                           'Please enter your M-Pesa PIN.'
            }
        else:
            return {
                'success': False,
                'message': result.get('ResponseDescription',
                                      'Payment request failed.')
            }

    except Exception as e:
        print(f"[MPESA STK ERROR] {str(e)}")
        return {
            'success': False,
            'message': 'M-Pesa service temporarily unavailable.'
        }


def send_b2c_payment(phone_number, amount, transaction_id, remarks):
    """
    Sends money directly to a seller's M-Pesa number.

    B2C = Business to Customer payment.
    This is how Zuno pays sellers after a deal completes.

    Called when:
    - Buyer approves during inspection window
    - Inspection window expires (auto-complete)
    - Admin resolves dispute in seller's favor

    Args:
        phone_number   : seller's M-Pesa number
        amount         : amount to send in KES
        transaction_id : Zuno transaction reference
        remarks        : description of payment

    Returns dict with success status and message.
    """
    token = get_access_token()
    if not token:
        return {
            'success': False,
            'message': 'Failed to connect to M-Pesa.'
        }

    phone = _format_phone(phone_number)
    if not phone:
        return {
            'success': False,
            'message': 'Invalid phone number.'
        }

    environment = os.getenv('MPESA_ENVIRONMENT', 'sandbox')
    shortcode = os.getenv('MPESA_SHORTCODE')
    initiator = os.getenv('MPESA_B2C_INITIATOR')
    security_credential = os.getenv('MPESA_B2C_SECURITY_CREDENTIAL')
    callback_url = os.getenv('MPESA_CALLBACK_URL', '').replace(
        'mpesa/callback', 'mpesa/b2c/result'
    )

    amount_int = int(round(float(amount)))

    if environment == 'production':
        url = 'https://api.safaricom.co.ke/mpesa/b2c/v3/paymentrequest'
    else:
        url = 'https://sandbox.safaricom.co.ke/mpesa/b2c/v3/paymentrequest'

    payload = {
        # Name of the initiator making the request
        'InitiatorName': initiator,

        # Encrypted security credential
        'SecurityCredential': security_credential,

        # Type of transaction
        # BusinessPayment = paying an individual
        'CommandID': 'BusinessPayment',

        # Amount to send
        'Amount': amount_int,

        # Your shortcode (sender)
        'PartyA': shortcode,

        # Seller's phone number (recipient)
        'PartyB': phone,

        # Short reference
        'Remarks': remarks[:100],

        # Where Safaricom sends the payment result
        'ResultURL': callback_url,

        # Where Safaricom sends if queue times out
        'QueueTimeOutURL': callback_url,

        # Optional extra info
        'Occasion': f"Zuno payout {transaction_id}"
    }

    try:
        response = requests.post(
            url,
            json=payload,
            headers={
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json'
            }
        )

        result = response.json()
        print(f"[MPESA B2C] Response: {result}")

        if result.get('ResponseCode') == '0':
            return {
                'success': True,
                'conversation_id': result.get('ConversationID'),
                'message': f"Payment of Ksh {amount_int} "
                           f"sent to {phone_number}"
            }
        else:
            return {
                'success': False,
                'message': result.get('ResponseDescription',
                                      'Payment failed.')
            }

    except Exception as e:
        print(f"[MPESA B2C ERROR] {str(e)}")
        return {
            'success': False,
            'message': 'M-Pesa service temporarily unavailable.'
        }


def _format_phone(phone):
    """
    Private helper — converts any Kenyan phone format
    to the 254XXXXXXXXX format M-Pesa requires.

    Returns None if format is unrecognizable.
    """
    if not phone:
        return None

    phone = str(phone).strip().replace(' ', '').replace('-', '')

    if phone.startswith('+254'):
        return phone[1:]
    elif phone.startswith('0') and len(phone) == 10:
        return '254' + phone[1:]
    elif phone.startswith('254') and len(phone) == 12:
        return phone

    return None