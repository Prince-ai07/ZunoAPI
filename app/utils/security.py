# app/utils/security.py
#
# Cryptographic security for the Zuno platform.
#
# The most important thing this file does:
# Every payment link Zuno generates contains a CRYPTOGRAPHIC SIGNATURE.
# This signature is mathematically tied to the transaction details
# using a secret key that only Zuno's server knows.
#
# Why does this matter?
# Without this, a con man could generate fake payment links that
# look identical to Zuno links but send money to his account.
# With this, any fake link fails signature verification instantly
# and is rejected before the buyer ever sees a payment page.
#
# This is the same technology banks use to secure transactions.

import hmac
import hashlib
import os
from collections import defaultdict
from datetime import datetime, timedelta
from flask import request, jsonify

# Our secret signing key — loaded from .env
# If this key is ever exposed, regenerate it immediately
SIGNING_SECRET = os.getenv('LINK_SIGNING_SECRET', '')

# In-memory store for rate limiting
# Maps IP address → list of request timestamps
_request_log = defaultdict(list)


def generate_link_signature(transaction_id, seller_id, amount):
    """
    Creates a cryptographic signature for a Zuno payment link.

    HOW IT WORKS:
    1. We combine transaction details into a single string
    2. We run it through HMAC-SHA256 with our secret key
    3. The output is a unique 32-character hex string

    The critical property of HMAC-SHA256:
    Without knowing SIGNING_SECRET, it is computationally
    impossible to generate a valid signature — even if you
    know the algorithm and the inputs.

    So a con man knows the format is:
    /pay/ZNO-20260601-A3K9P?sig=XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
    But he CANNOT generate a valid XXXXXXXX without our secret key.
    Every fake link he creates will fail verification.
    """
    message = f"{transaction_id}:{seller_id}:{amount}"

    signature = hmac.new(
        SIGNING_SECRET.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    # Return first 32 characters — still 128 bits of security
    # Short enough for a clean URL, long enough to be unguessable
    return signature[:32]


def verify_link_signature(transaction_id, seller_id, amount, provided_sig):
    """
    Verifies that a payment link signature is genuine.

    Called EVERY time a buyer opens a payment link.
    If the signature doesn't match → reject immediately.

    Uses hmac.compare_digest() instead of ==
    WHY: Regular == comparison can leak timing information.
    An attacker can measure tiny differences in how long == takes
    to guess the signature one character at a time.
    compare_digest() always takes the same time, preventing this.

    Returns:
        (True, None)           if signature is valid
        (False, error_message) if signature is invalid
    """
    if not provided_sig:
        return False, "Missing security token — this link is incomplete"

    expected = generate_link_signature(transaction_id, seller_id, amount)

    if not hmac.compare_digest(expected, str(provided_sig)):
        return False, "Invalid security token — this link may be fraudulent"

    return True, None


def build_payment_link(transaction_id, seller_id, amount):
    """
    Builds a complete, cryptographically signed Zuno payment link.

    THIS IS THE ONLY FUNCTION THAT SHOULD CREATE PAYMENT LINKS.
    Never build payment links manually anywhere else in the codebase.

    Output example:
    https://zuno.co.ke/pay/ZNO-20260601-A3K9P?sig=a3f9k2p8x7q1b4d8c2e5f6g7h8i9j0k1
    """
    base = os.getenv('BASE_URL', 'http://localhost:5000')
    sig = generate_link_signature(transaction_id, seller_id, amount)
    return f"{base}/pay/{transaction_id}?sig={sig}"


def is_rate_limited(ip_address, max_requests=20, window_minutes=1):
    """
    Checks if an IP address is making too many requests.

    Default limit: 20 requests per minute per IP address.
    If exceeded, the IP is blocked from making more requests.

    WHY THIS MATTERS:
    Without rate limiting, a bot can make thousands of requests
    per second trying to guess valid transaction IDs or
    forge payment link signatures.
    With rate limiting, after 20 attempts the bot is blocked.

    In production we would use Redis for this instead of
    in-memory storage, so limits persist across server restarts.

    Returns True if the IP should be blocked.
    """
    now = datetime.now()
    cutoff = now - timedelta(minutes=window_minutes)

    # Remove requests older than our time window
    _request_log[ip_address] = [
        t for t in _request_log[ip_address] if t > cutoff
    ]

    # Block if over limit
    if len(_request_log[ip_address]) >= max_requests:
        return True

    # Record this request and allow it through
    _request_log[ip_address].append(now)
    return False


def check_rate_limit():
    """
    Convenience function for use inside route handlers.
    Call this at the top of any sensitive endpoint.

    Returns a 429 error response if rate limited, None if OK.

    Usage in a route:
        rate_limit_error = check_rate_limit()
        if rate_limit_error:
            return rate_limit_error
    """
    if is_rate_limited(request.remote_addr):
        from app.utils.helpers import log_security_event
        log_security_event(
            event_type='RATE_LIMIT_EXCEEDED',
            details=f"IP {request.remote_addr} exceeded rate limit",
            ip_address=request.remote_addr
        )
        return jsonify({
            'error': 'Too many requests. Please slow down.'
        }), 429

    return None  # not rate limited, proceed normally