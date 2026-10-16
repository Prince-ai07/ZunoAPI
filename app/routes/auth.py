# app/routes/auth.py
#
# Authentication endpoints for Zuno.
# These are the first routes every seller interacts with.
#
# ENDPOINTS IN THIS FILE:
#
# POST /api/auth/register        → Create a new seller account
# POST /api/auth/send-otp        → Send OTP to phone for verification
# POST /api/auth/verify-otp      → Verify the OTP code entered
# POST /api/auth/login           → Login and receive JWT token
# GET  /api/auth/profile         → Get logged-in user's profile
# PUT  /api/auth/profile         → Update profile details
#
# SECURITY NOTES:
# - Passwords are NEVER stored in plain text — always hashed with bcrypt
# - Login errors use generic messages — never reveal if phone exists
# - JWT tokens expire after 24 hours
# - OTP codes expire after 10 minutes and max 3 attempts

from flask import Blueprint, request, jsonify
from flask_jwt_extended import (
    create_access_token,
    jwt_required,
    get_jwt_identity
)
from app import mysql, bcrypt
from app.utils.validators import (
    validate_phone,
    validate_password,
    validate_required_fields
)
from app.utils.security import check_rate_limit
from app.services.sms import send_otp
from datetime import datetime, timedelta
import random

auth_bp = Blueprint('auth', __name__)


# ══════════════════════════════════════════════════════════════
# ENDPOINT 1 — REGISTER
# ══════════════════════════════════════════════════════════════

@auth_bp.route('/register', methods=['POST'])
def register():
    """
    POST /api/auth/register

    Creates a new seller account on Zuno.

    What happens step by step:
    1. Validate all required fields are present
    2. Validate phone number is a real Kenyan number
    3. Validate password meets strength requirements
    4. Check the phone number is not already registered
    5. Hash the password (never store plain text)
    6. Save the new user to the database
    7. Create a blank reputation record for this seller
    8. Send an OTP to verify their phone number

    Expected request body:
    {
        "full_name": "Jane Muthoni",
        "phone_number": "0712345678",
        "password": "securepass1",
        "business_name": "Jane's Boutique",  (optional)
        "email": "jane@gmail.com"             (optional)
    }

    Success response:
    {
        "message": "Account created. Please verify your phone number.",
        "user_id": 1,
        "next_step": "verify_phone"
    }
    """

    # ── Rate limit check ──────────────────────────────────────
    # Prevents bots from creating thousands of accounts
    rate_limit_error = check_rate_limit()
    if rate_limit_error:
        return rate_limit_error

    data = request.get_json()

    # ── Step 1: Validate required fields ─────────────────────
    valid, error = validate_required_fields(
        data, ['full_name', 'phone_number', 'password']
    )
    if not valid:
        return jsonify({'error': error}), 400

    # ── Step 2: Validate phone format ─────────────────────────
    valid, error = validate_phone(data['phone_number'])
    if not valid:
        return jsonify({'error': error}), 400

    # ── Step 3: Validate password strength ────────────────────
    valid, error = validate_password(data['password'])
    if not valid:
        return jsonify({'error': error}), 400

    # ── Step 4: Check phone not already registered ────────────
    cursor = mysql.connection.cursor()
    cursor.execute(
        "SELECT id FROM users WHERE phone_number = %s",
        (data['phone_number'],)
    )
    if cursor.fetchone():
        cursor.close()
        return jsonify({
            'error': 'An account with this phone number already exists'
        }), 409  # 409 = Conflict

    # ── Step 5: Hash the password ─────────────────────────────
    # bcrypt turns "mypassword123" into something like:
    # "$2b$12$EixZaYVK1fsbw1ZfbX3OXe.PkE9pDEkEm9S1XCFGO7..."
    # Even if the database is stolen, passwords cannot be recovered
    password_hash = bcrypt.generate_password_hash(
        data['password']
    ).decode('utf-8')

    # ── Step 6: Save user to database ─────────────────────────
    cursor.execute("""
        INSERT INTO users
            (full_name, phone_number, email, password_hash, business_name)
        VALUES (%s, %s, %s, %s, %s)
    """, (
        data['full_name'].strip(),
        data['phone_number'],
        data.get('email', None),
        password_hash,
        data.get('business_name', None)
    ))

    # Get the ID of the newly created user
    user_id = cursor.lastrowid
    mysql.connection.commit()

    # ── Step 7: Create blank reputation record ────────────────
    # Every seller starts with a clean reputation record
    # This record gets updated automatically as they do deals
    cursor.execute(
        "INSERT INTO seller_reputation (seller_id) VALUES (%s)",
        (user_id,)
    )
    mysql.connection.commit()
    cursor.close()

    # ── Step 8: Send OTP to verify phone ─────────────────────
    # We call our own send-otp endpoint logic directly
    # The user must verify their phone before they can log in
    _generate_and_send_otp(data['phone_number'])

    # ── Step 9: Send email verification if email provided ─────
    # Email verification is optional at registration
    # but strongly encouraged
    if data.get('email'):
        from app.services.email import send_verification_email
        send_verification_email(
            user_id,
            data['email'],
            data['full_name']
        )
    
    return jsonify({
        'message': 'Account created successfully. '
                   'Please verify your phone number via SMS'
                   + (' and check your email for a verification link.'
                      if data.get('email') else '.'),
        'user_id': user_id,
        'next_step': 'verify_phone'
    }), 201


# ══════════════════════════════════════════════════════════════
# ENDPOINT 2 — SEND OTP
# ══════════════════════════════════════════════════════════════

@auth_bp.route('/send-otp', methods=['POST'])
def send_otp_endpoint():
    """
    POST /api/auth/send-otp

    Sends or resends an OTP code to a phone number.
    Used during registration and if the user requests a resend.

    Expected request body:
    {
        "phone_number": "0712345678"
    }
    """
    rate_limit_error = check_rate_limit()
    if rate_limit_error:
        return rate_limit_error

    data = request.get_json()

    valid, error = validate_phone(data.get('phone_number'))
    if not valid:
        return jsonify({'error': error}), 400

    success = _generate_and_send_otp(data['phone_number'])

    if not success:
        return jsonify({'error': 'Failed to send OTP. Please try again.'}), 500

    return jsonify({
        'message': 'Verification code sent to your phone.',
        'expires_in': '10 minutes'
    }), 200


# ══════════════════════════════════════════════════════════════
# ENDPOINT 3 — VERIFY OTP
# ══════════════════════════════════════════════════════════════

@auth_bp.route('/verify-otp', methods=['POST'])
def verify_otp():
    """
    POST /api/auth/verify-otp

    Verifies the OTP code the user received via SMS.
    On success, marks their phone as verified in the database.

    Security rules:
    - OTP expires after 10 minutes
    - Maximum 3 wrong attempts before lockout
    - Each wrong attempt is counted and saved

    Expected request body:
    {
        "phone_number": "0712345678",
        "otp_code": "847291"
    }
    """
    rate_limit_error = check_rate_limit()
    if rate_limit_error:
        return rate_limit_error

    data = request.get_json()

    valid, error = validate_required_fields(
        data, ['phone_number', 'otp_code']
    )
    if not valid:
        return jsonify({'error': error}), 400

    cursor = mysql.connection.cursor()

    # Find the OTP record for this phone number
    cursor.execute("""
        SELECT * FROM otp_verifications
        WHERE phone_number = %s
    """, (data['phone_number'],))

    otp_record = cursor.fetchone()

    # ── Check OTP exists ──────────────────────────────────────
    if not otp_record:
        cursor.close()
        return jsonify({
            'error': 'No verification code found. Please request a new one.'
        }), 404

    # ── Check not too many wrong attempts ─────────────────────
    if otp_record['attempts'] >= 3:
        cursor.close()
        return jsonify({
            'error': 'Too many incorrect attempts. '
                     'Please request a new verification code.'
        }), 429

    # ── Check OTP has not expired ─────────────────────────────
    if datetime.now() > otp_record['expires_at']:
        cursor.close()
        return jsonify({
            'error': 'Verification code has expired. '
                     'Please request a new one.'
        }), 400

    # ── Check OTP code matches ────────────────────────────────
    if str(otp_record['otp_code']) != str(data['otp_code']).strip():
        # Increment the attempt counter
        cursor.execute("""
            UPDATE otp_verifications
            SET attempts = attempts + 1
            WHERE phone_number = %s
        """, (data['phone_number'],))
        mysql.connection.commit()
        cursor.close()

        attempts_left = 3 - (otp_record['attempts'] + 1)
        return jsonify({
            'error': f"Incorrect code. {attempts_left} attempts remaining."
        }), 400

    # ── OTP is correct — mark phone as verified ───────────────
    now = datetime.now()

    # Mark OTP as used
    cursor.execute("""
        UPDATE otp_verifications
        SET verified_at = %s
        WHERE phone_number = %s
    """, (now, data['phone_number']))

    # Mark user's account as phone-verified
    # We use verification_status PENDING because they still
    # need to submit ID documents to become fully verified
    cursor.execute("""
        UPDATE users
        SET verification_status = 'PENDING'
        WHERE phone_number = %s
        AND verification_status = 'UNVERIFIED'
    """, (data['phone_number'],))

    mysql.connection.commit()
    cursor.close()

    return jsonify({
        'message': 'Phone number verified successfully. You can now log in.',
        'next_step': 'login'
    }), 200


# ══════════════════════════════════════════════════════════════
# ENDPOINT 4 — LOGIN
# ══════════════════════════════════════════════════════════════

@auth_bp.route('/login', methods=['POST'])
def login():
    """
    POST /api/auth/login

    Authenticates a seller and returns a JWT token.

    The JWT token must be included in the Authorization header
    of every subsequent request to protected endpoints:
        Authorization: Bearer <token>

    Security note:
    We return the SAME error message whether the phone number
    doesn't exist OR the password is wrong. This is intentional —
    telling attackers which one is wrong helps them narrow
    down valid accounts.

    Expected request body:
    {
        "phone_number": "0712345678",
        "password": "securepass1"
    }

    Success response:
    {
        "message": "Login successful",
        "token": "eyJhbGc...",
        "user": { ... }
    }
    """
    rate_limit_error = check_rate_limit()
    if rate_limit_error:
        return rate_limit_error

    data = request.get_json()

    valid, error = validate_required_fields(
        data, ['phone_number', 'password']
    )
    if not valid:
        return jsonify({'error': error}), 400

    # Find user by phone number
    cursor = mysql.connection.cursor()
    cursor.execute(
        "SELECT * FROM users WHERE phone_number = %s",
        (data['phone_number'],)
    )
    user = cursor.fetchone()
    cursor.close()

    # ── Check user exists AND password is correct ─────────────
    # We check both in one condition deliberately —
    # never reveal which one failed
    if not user or not bcrypt.check_password_hash(
        user['password_hash'], data['password']
    ):
        return jsonify({
            'error': 'Invalid phone number or password'
        }), 401  # 401 = Unauthorized

    # ── Check account is active ───────────────────────────────
    if not user['is_active']:
        return jsonify({
            'error': 'Your account has been suspended. '
                     'Please contact support@zuno.co.ke'
        }), 403  # 403 = Forbidden

    # ── Create JWT token ──────────────────────────────────────
    # We embed key user info inside the token.
    # This info is readable by anyone who has the token,
    # so we only include non-sensitive identity fields.
    # The token is SIGNED so it cannot be tampered with.
    # JWT-Extended 4.x requires identity to be a string
    # We store just the user ID and fetch full details
    # from the database whenever we need them
    token = create_access_token(identity=str(user['id']))

    return jsonify({
        'message': 'Login successful',
        'token': token,
        'user': {
            'id': user['id'],
            'full_name': user['full_name'],
            'phone_number': user['phone_number'],
            'business_name': user['business_name'],
            'role': user['role'],
            'is_verified': user['is_verified'],
            'verification_status': user['verification_status'],
            'seller_tier': user['seller_tier']
        }
    }), 200


# ══════════════════════════════════════════════════════════════
# ENDPOINT 5 — GET PROFILE
# ══════════════════════════════════════════════════════════════

@auth_bp.route('/profile', methods=['GET'])
@jwt_required()
def get_profile():
    """
    GET /api/auth/profile

    Returns the full profile of the currently logged-in seller,
    including their reputation metrics and trust score.

    Requires: Authorization: Bearer <token> in request headers.

    @jwt_required() decorator does the work —
    if no valid token is provided, Flask automatically
    returns a 401 error before our code even runs.
    """
    # get_jwt_identity() reads the user info we stored in the token
    # get_jwt_identity() now returns a string user ID
    user_id = get_jwt_identity()
    
    cursor = mysql.connection.cursor()
    cursor.execute("""
        SELECT
            u.id, u.full_name, u.phone_number, u.email,
            u.business_name, u.bio, u.profile_photo,
            u.role, u.is_verified, u.verification_status,
            u.seller_tier, u.created_at,
            r.completion_rate, r.cancellation_rate,
            r.dispute_rate, r.late_delivery_rate,
            r.average_rating, r.total_transactions,
            r.trust_score, r.trust_level,
            r.is_visible_in_search, r.strike_count
        FROM users u
        LEFT JOIN seller_reputation r ON r.seller_id = u.id
        WHERE u.id = %s
    """, (user_id,))

    user = cursor.fetchone()
    cursor.close()

    if not user:
        return jsonify({'error': 'User not found'}), 404

    # Convert datetime to string for JSON serialization
    if user.get('created_at'):
        user['created_at'] = str(user['created_at'])

    return jsonify({'user': user}), 200


# ══════════════════════════════════════════════════════════════
# ENDPOINT 6 — UPDATE PROFILE
# ══════════════════════════════════════════════════════════════

@auth_bp.route('/profile', methods=['PUT'])
@jwt_required()
def update_profile():
    """
    PUT /api/auth/profile

    Updates the logged-in seller's profile information.
    Only allows updating safe fields —
    phone number and role cannot be changed here.

    Expected request body (all fields optional):
    {
        "full_name": "Jane Muthoni Kamau",
        "business_name": "Jane's Fashion House",
        "bio": "Premium fashion seller based in Nairobi",
        "email": "jane@gmail.com"
    }
    """
    user_id = get_jwt_identity()
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    # Only allow these fields to be updated
    # Never allow phone_number, role, or is_verified
    # to be changed through this endpoint
    allowed_fields = ['full_name', 'business_name', 'bio', 'email']
    updates = {}

    for field in allowed_fields:
        if field in data and data[field] is not None:
            updates[field] = str(data[field]).strip()

    if not updates:
        return jsonify({'error': 'No valid fields to update'}), 400

    # Build dynamic SQL update statement
    # This safely handles any combination of fields being updated
    set_clause = ', '.join([f"{field} = %s" for field in updates.keys()])
    values = list(updates.values()) + [user_id]
    cursor = mysql.connection.cursor()
    cursor.execute(
        f"UPDATE users SET {set_clause} WHERE id = %s",
        values
    )
    mysql.connection.commit()
    cursor.close()

    return jsonify({
        'message': 'Profile updated successfully'
    }), 200


def get_current_user():
    """
    Helper function that fetches the full user record
    for whoever is currently logged in.
    
    Since we now store only the user ID in the JWT token,
    we use this function whenever we need the full user
    details (phone number, role, tier etc.) inside a
    protected route.
    
    Usage in any protected route:
        user = get_current_user()
        if not user:
            return jsonify({'error': 'User not found'}), 404
    """
    user_id = get_jwt_identity()
    
    cursor = mysql.connection.cursor()
    cursor.execute(
        "SELECT * FROM users WHERE id = %s AND is_active = TRUE",
        (user_id,)
    )
    user = cursor.fetchone()
    cursor.close()
    
    return user


# ══════════════════════════════════════════════════════════════
# ENDPOINT 7 — VERIFY EMAIL
# ══════════════════════════════════════════════════════════════

@auth_bp.route('/verify-email', methods=['POST'])
def verify_email():
    """
    POST /api/auth/verify-email
    
    Called when a user clicks the verification link in their email.
    The frontend extracts the token from the URL and sends it here.
    
    Expected request body:
    {
        "token": "a3F9k2P8x7Q1b4D8c2E5f6G7h8I9j0K1L2M3n4O5"
    }
    """
    data = request.get_json()
    
    if not data or not data.get('token'):
        return jsonify({'error': 'Verification token is required'}), 400
    
    token = data['token'].strip()
    
    cursor = mysql.connection.cursor()
    
    # Find the verification record for this token
    cursor.execute("""
        SELECT ev.*, u.full_name, u.email as user_email
        FROM email_verifications ev
        JOIN users u ON u.id = ev.user_id
        WHERE ev.token = %s
    """, (token,))
    
    record = cursor.fetchone()
    
    # ── Token not found ───────────────────────────────────────
    if not record:
        cursor.close()
        return jsonify({
            'error': 'Invalid verification link. '
                     'Please request a new one.'
        }), 404
    
    # ── Token already used ────────────────────────────────────
    if record['verified_at'] is not None:
        cursor.close()
        return jsonify({
            'error': 'This link has already been used. '
                     'Your email is already verified.'
        }), 400
    
    # ── Token expired ─────────────────────────────────────────
    if datetime.now() > record['expires_at']:
        cursor.close()
        return jsonify({
            'error': 'This verification link has expired. '
                     'Please request a new one.'
        }), 400
    
    now = datetime.now()
    
    # ── Mark token as used ────────────────────────────────────
    cursor.execute("""
        UPDATE email_verifications
        SET verified_at = %s
        WHERE token = %s
    """, (now, token))
    
    # ── Mark user's email as verified ─────────────────────────
    cursor.execute("""
        UPDATE users
        SET email_verified = TRUE
        WHERE id = %s
    """, (record['user_id'],))
    
    mysql.connection.commit()
    cursor.close()
    
    # Send welcome email now that verification is complete
    from app.services.email import send_welcome_email
    send_welcome_email(record['email'], record['full_name'])
    
    return jsonify({
        'message': 'Email verified successfully. Welcome to Zuno!',
        'next_step': 'login'
    }), 200


# ══════════════════════════════════════════════════════════════
# ENDPOINT 8 — RESEND VERIFICATION EMAIL
# ══════════════════════════════════════════════════════════════

@auth_bp.route('/resend-verification-email', methods=['POST'])
def resend_verification_email():
    """
    POST /api/auth/resend-verification-email
    
    Resends the email verification link.
    Used when the original email expired or was not received.
    
    Expected request body:
    {
        "email": "jane@gmail.com"
    }
    """
    data = request.get_json()
    
    if not data or not data.get('email'):
        return jsonify({'error': 'Email address is required'}), 400
    
    cursor = mysql.connection.cursor()
    cursor.execute("""
        SELECT id, full_name, email, email_verified
        FROM users
        WHERE email = %s
    """, (data['email'].strip().lower(),))
    
    user = cursor.fetchone()
    cursor.close()
    
    # Always return success even if email not found
    # This prevents attackers from discovering which
    # emails are registered on the platform
    if not user:
        return jsonify({
            'message': 'If that email is registered, '
                       'a verification link has been sent.'
        }), 200
    
    if user['email_verified']:
        return jsonify({
            'message': 'This email is already verified. '
                       'You can log in normally.'
        }), 200
    
    # Send new verification email
    from app.services.email import send_verification_email
    send_verification_email(user['id'], user['email'], user['full_name'])
    
    return jsonify({
        'message': 'If that email is registered, '
                   'a verification link has been sent.'
    }), 200


# ══════════════════════════════════════════════════════════════
# ENDPOINT 9 — SUBMIT KYC DOCUMENTS
# ══════════════════════════════════════════════════════════════

@auth_bp.route('/kyc/submit', methods=['POST'])
@jwt_required()
def submit_kyc():
    """
    POST /api/auth/kyc/submit

    Seller submits KYC documents for verification.
    Accepts multipart/form-data with document uploads.

    This endpoint handles real file uploads.
    Files are uploaded directly to Cloudinary.

    Required form fields:
        national_id_number : e.g. "12345678"
        full_name          : legal name as on ID
        id_front           : photo file of ID front
        selfie             : photo file of selfie holding ID

    Optional form fields:
        id_back            : photo file of ID back
        date_of_birth      : e.g. "1995-06-15"
        business_name      : business trading name
        business_type      : INDIVIDUAL / SMALL_BUSINESS / COMPANY
        kra_pin            : KRA tax PIN
    """
    user_id = get_jwt_identity()

    # For file uploads we use request.form and request.files
    # NOT request.get_json() — JSON cannot carry files
    form = request.form
    files = request.files

    # Validate required form fields
    if not form.get('national_id_number'):
        return jsonify({'error': 'National ID number is required'}), 400

    if not form.get('full_name'):
        return jsonify({'error': 'Full name as on ID is required'}), 400

    # Validate required files
    if 'id_front' not in files:
        return jsonify({
            'error': 'Front photo of National ID is required'
        }), 400

    if 'selfie' not in files:
        return jsonify({
            'error': 'Selfie holding your ID is required'
        }), 400

    cursor = mysql.connection.cursor()

    # Check current verification status
    cursor.execute(
        "SELECT verification_status, full_name, phone_number "
        "FROM users WHERE id = %s",
        (user_id,)
    )
    user = cursor.fetchone()

    if not user:
        cursor.close()
        return jsonify({'error': 'User not found'}), 404

    # Already verified — no need to resubmit
    if user['verification_status'] == 'VERIFIED':
        cursor.close()
        return jsonify({
            'message': 'Your account is already verified.',
            'status': 'VERIFIED'
        }), 200

    # Upload documents to Cloudinary
    from app.services.upload import upload_id_document

    # Upload ID front
    id_front_result = upload_id_document(
        files['id_front'],
        user_id,
        'id_front'
    )
    if not id_front_result['success']:
        cursor.close()
        return jsonify({
            'error': 'Failed to upload ID photo. Please try again.'
        }), 500

    # Upload selfie
    selfie_result = upload_id_document(
        files['selfie'],
        user_id,
        'selfie'
    )
    if not selfie_result['success']:
        cursor.close()
        return jsonify({
            'error': 'Failed to upload selfie. Please try again.'
        }), 500

    # Upload ID back if provided
    id_back_url = None
    if 'id_back' in files:
        id_back_result = upload_id_document(
            files['id_back'],
            user_id,
            'id_back'
        )
        if id_back_result['success']:
            id_back_url = id_back_result['url']

    # Check if submission already exists
    cursor.execute(
        "SELECT id FROM kyc_submissions WHERE seller_id = %s",
        (user_id,)
    )
    existing = cursor.fetchone()

    if existing:
        # Update existing submission (resubmission after rejection)
        cursor.execute("""
            UPDATE kyc_submissions SET
                full_name = %s,
                national_id_number = %s,
                date_of_birth = %s,
                id_front_url = %s,
                id_back_url = %s,
                selfie_url = %s,
                business_name = %s,
                business_type = %s,
                kra_pin = %s,
                status = 'RESUBMITTED',
                submitted_at = %s,
                reviewed_at = NULL,
                reviewed_by = NULL,
                review_notes = NULL,
                rejection_reason = NULL
            WHERE seller_id = %s
        """, (
            form.get('full_name').strip(),
            form.get('national_id_number').strip(),
            form.get('date_of_birth', None),
            id_front_result['url'],
            id_back_url,
            selfie_result['url'],
            form.get('business_name', None),
            form.get('business_type', 'INDIVIDUAL'),
            form.get('kra_pin', None),
            datetime.now(),
            user_id
        ))
    else:
        # New submission
        cursor.execute("""
            INSERT INTO kyc_submissions (
                seller_id, full_name, national_id_number,
                date_of_birth, id_front_url, id_back_url,
                selfie_url, business_name, business_type, kra_pin
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            user_id,
            form.get('full_name').strip(),
            form.get('national_id_number').strip(),
            form.get('date_of_birth', None),
            id_front_result['url'],
            id_back_url,
            selfie_result['url'],
            form.get('business_name', None),
            form.get('business_type', 'INDIVIDUAL'),
            form.get('kra_pin', None)
        ))

    # Update user verification status to PENDING
    cursor.execute("""
        UPDATE users SET
            verification_status = 'PENDING',
            national_id = %s,
            id_document_url = %s,
            business_name = COALESCE(%s, business_name)
        WHERE id = %s
    """, (
        form.get('national_id_number').strip(),
        id_front_result['url'],
        form.get('business_name', None),
        user_id
    ))

    mysql.connection.commit()
    cursor.close()

    # Notify all admins via SMS
    _notify_admins_of_kyc(user['full_name'], user['phone_number'])

    return jsonify({
        'message': 'KYC documents submitted successfully. '
                   'Our team will review within 24-48 hours.',
        'status': 'PENDING',
        'documents_uploaded': {
            'id_front': '✅ Uploaded',
            'id_back': '✅ Uploaded' if id_back_url else '⚠️ Not provided',
            'selfie': '✅ Uploaded'
        },
        'next_steps': [
            'Wait for admin review (24-48 hours)',
            'You will receive an SMS with the decision',
            'Once approved you get the Verified Seller badge',
            'Your transaction limit increases to Ksh 10,000'
        ]
    }), 200


@auth_bp.route('/kyc/status', methods=['GET'])
@jwt_required()
def get_kyc_status():
    """
    GET /api/auth/kyc/status

    Returns the seller's current KYC status.
    Sellers use this to track their verification progress.
    """
    user_id = get_jwt_identity()

    cursor = mysql.connection.cursor()
    cursor.execute("""
        SELECT
            k.status,
            k.submitted_at,
            k.reviewed_at,
            k.rejection_reason,
            k.review_notes,
            u.verification_status,
            u.is_verified,
            u.seller_tier
        FROM users u
        LEFT JOIN kyc_submissions k ON k.seller_id = u.id
        WHERE u.id = %s
    """, (user_id,))

    result = cursor.fetchone()
    cursor.close()

    if not result:
        return jsonify({'error': 'User not found'}), 404

    # Convert timestamps
    for field in ['submitted_at', 'reviewed_at']:
        if result.get(field):
            result[field] = str(result[field])

    # Build status message based on current state
    status_messages = {
        'UNVERIFIED': 'You have not submitted KYC documents yet.',
        'PENDING': 'Your documents are awaiting review.',
        'SUBMITTED': 'Your documents have been submitted.',
        'UNDER_REVIEW': 'An admin is currently reviewing your documents.',
        'RESUBMITTED': 'Your resubmission is awaiting review.',
        'VERIFIED': 'Your account is fully verified. ✅',
        'REJECTED': 'Your verification was rejected. '
                    'Please review the reason and resubmit.'
    }

    current_status = result.get('status') or result['verification_status']
    message = status_messages.get(current_status, 'Status unknown')

    return jsonify({
        'kyc_status': current_status,
        'message': message,
        'is_verified': result['is_verified'],
        'seller_tier': result['seller_tier'],
        'submitted_at': result.get('submitted_at'),
        'reviewed_at': result.get('reviewed_at'),
        'rejection_reason': result.get('rejection_reason'),
        'review_notes': result.get('review_notes'),
        'tier_benefits': {
            0: 'Max deal: Ksh 1,000',
            1: 'Max deal: Ksh 10,000 + Verified Badge',
            2: 'Max deal: Ksh 100,000 + Full access'
        }.get(result['seller_tier'], 'Unknown tier')
    }), 200


def _notify_admins_of_kyc(seller_name, seller_phone):
    """
    Private helper — notifies all admin accounts
    when a new KYC submission arrives.
    """
    from app.services.sms import send_sms
    from app import mysql

    cursor = mysql.connection.cursor()
    cursor.execute(
        "SELECT phone_number FROM users WHERE role = 'admin' AND is_active = TRUE"
    )
    admins = cursor.fetchall()
    cursor.close()

    for admin in admins:
        send_sms(
            admin['phone_number'],
            f"Zuno KYC: New verification submission from "
            f"{seller_name} ({seller_phone}). "
            f"Log in to review at /api/admin/kyc/pending"
        )

# ══════════════════════════════════════════════════════════════
# PRIVATE HELPER FUNCTION
# ══════════════════════════════════════════════════════════════

def _generate_and_send_otp(phone_number):
    """
    Internal helper — generates a 6-digit OTP, saves it to
    the database with a 10-minute expiry, and sends it via SMS.

    The underscore prefix (_) means this function is private —
    it should only be called from within this file.

    Returns True if OTP was sent, False if something failed.
    """
    # Generate a random 6-digit code
    # e.g. 847291
    otp_code = str(random.randint(100000, 999999))

    # Set expiry to 10 minutes from now
    expires_at = datetime.now() + timedelta(minutes=10)

    cursor = mysql.connection.cursor()

    # INSERT OR UPDATE — if a record exists for this phone,
    # replace it with the new OTP (handles resend case)
    cursor.execute("""
        INSERT INTO otp_verifications
            (phone_number, otp_code, expires_at, attempts)
        VALUES (%s, %s, %s, 0)
        ON DUPLICATE KEY UPDATE
            otp_code = %s,
            expires_at = %s,
            attempts = 0,
            verified_at = NULL
    """, (phone_number, otp_code, expires_at, otp_code, expires_at))

    mysql.connection.commit()
    cursor.close()

    # Send the OTP via SMS
    return send_otp(phone_number, otp_code)