# app/routes/admin.py
#
# Admin Dashboard for Zuno.
#
# ALL routes here require admin role.
# Regular sellers cannot access any of these endpoints.
#
# ENDPOINTS:
# GET  /api/admin/disputes              → all open disputes
# GET  /api/admin/disputes/<id>        → dispute detail with evidence
# POST /api/admin/disputes/<id>/resolve → make final decision
# GET  /api/admin/transactions          → all transactions
# GET  /api/admin/metrics               → platform statistics
# POST /api/admin/users/<id>/verify     → approve seller verification
# POST /api/admin/users/<id>/suspend    → suspend a user account

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app import mysql
from app.utils.helpers import log_transaction_action
from app.utils.validators import validate_required_fields
from app.services.sms import send_sms
from datetime import datetime

admin_bp = Blueprint('admin', __name__)


def require_admin(f):
    """
    Decorator that checks the logged-in user is an admin.
    Applied to every route in this file.

    If a regular seller tries to access admin routes,
    they get a 403 Forbidden response.

    This is our role-based access control (RBAC) system.
    """
    from functools import wraps

    @wraps(f)
    def decorated(*args, **kwargs):
        user_id = get_jwt_identity()

        cursor = mysql.connection.cursor()
        cursor.execute(
            "SELECT role FROM users WHERE id = %s",
            (user_id,)
        )
        user = cursor.fetchone()
        cursor.close()

        if not user or user['role'] != 'admin':
            return jsonify({
                'error': 'Admin access required. '
                         'You are not authorized for this action.'
            }), 403

        return f(*args, **kwargs)
    return decorated


# ══════════════════════════════════════════════════════════════
# ENDPOINT 1 — GET ALL OPEN DISPUTES
# ══════════════════════════════════════════════════════════════

@admin_bp.route('/disputes', methods=['GET'])
@jwt_required()
@require_admin
def get_all_disputes():
    """
    GET /api/admin/disputes
    GET /api/admin/disputes?status=ESCALATED

    Returns all disputes for admin review.
    Sorted by urgency — escalated disputes first,
    then by resolution deadline (soonest first).

    Admin uses this as their work queue.
    """
    status_filter = request.args.get('status')

    cursor = mysql.connection.cursor()

    if status_filter:
        cursor.execute("""
            SELECT
                d.*,
                t.item_name, t.amount,
                t.buyer_phone, t.buyer_name,
                u.full_name as seller_name,
                u.phone_number as seller_phone,
                u.business_name
            FROM disputes d
            JOIN transactions t ON t.id = d.transaction_id
            JOIN users u ON u.id = t.seller_id
            WHERE d.status = %s
            ORDER BY
                CASE d.status
                    WHEN 'ESCALATED' THEN 1
                    WHEN 'UNDER_REVIEW' THEN 2
                    WHEN 'EVIDENCE_COLLECTION' THEN 3
                    WHEN 'NEGOTIATING' THEN 4
                    ELSE 5
                END,
                d.resolution_deadline ASC
        """, (status_filter.upper(),))
    else:
        cursor.execute("""
            SELECT
                d.*,
                t.item_name, t.amount,
                t.buyer_phone, t.buyer_name,
                u.full_name as seller_name,
                u.phone_number as seller_phone,
                u.business_name
            FROM disputes d
            JOIN transactions t ON t.id = d.transaction_id
            JOIN users u ON u.id = t.seller_id
            WHERE d.status != 'RESOLVED'
            ORDER BY
                CASE d.status
                    WHEN 'ESCALATED' THEN 1
                    WHEN 'UNDER_REVIEW' THEN 2
                    WHEN 'EVIDENCE_COLLECTION' THEN 3
                    WHEN 'NEGOTIATING' THEN 4
                    ELSE 5
                END,
                d.resolution_deadline ASC
        """)

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


# ══════════════════════════════════════════════════════════════
# ENDPOINT 2 — RESOLVE DISPUTE (Admin Final Decision)
# ══════════════════════════════════════════════════════════════

@admin_bp.route('/disputes/<int:dispute_id>/resolve',
                methods=['POST'])
@jwt_required()
@require_admin
def resolve_dispute(dispute_id):
    """
    POST /api/admin/disputes/<dispute_id>/resolve

    Admin makes the final binding decision on a dispute.
    This cannot be appealed — it is absolute.

    After resolution:
    - Dispute marked as RESOLVED
    - Funds released to winner OR split
    - Both parties notified via SMS
    - Seller reputation updated
    - Payout record created

    Expected body:
    {
        "resolution": "REFUND_BUYER",
        "admin_notes": "Seller sent wrong size. Evidence from buyer
                        clearly shows size 41 was delivered despite
                        contract specifying size 42. Full refund issued.",
        "split_buyer_amount": null,
        "split_seller_amount": null
    }

    resolution options:
        REFUND_BUYER      → full refund to buyer
        RELEASE_TO_SELLER → full payment to seller
        SPLIT             → split the amount (provide split amounts)
    """
    admin_id = get_jwt_identity()
    data = request.get_json()

    valid, error = validate_required_fields(
        data, ['resolution', 'admin_notes']
    )
    if not valid:
        return jsonify({'error': error}), 400

    if data['resolution'] not in [
        'REFUND_BUYER', 'RELEASE_TO_SELLER', 'SPLIT'
    ]:
        return jsonify({
            'error': 'Resolution must be REFUND_BUYER, '
                     'RELEASE_TO_SELLER, or SPLIT'
        }), 400

    if data['resolution'] == 'SPLIT':
        if not data.get('split_buyer_amount') or \
           not data.get('split_seller_amount'):
            return jsonify({
                'error': 'split_buyer_amount and split_seller_amount '
                         'are required for SPLIT resolution'
            }), 400

    if len(data['admin_notes'].strip()) < 20:
        return jsonify({
            'error': 'Admin notes must be detailed '
                     '(minimum 20 characters). '
                     'Explain the reasoning for the decision.'
        }), 400

    cursor = mysql.connection.cursor()

    # Get dispute and transaction
    cursor.execute("""
        SELECT d.*, t.buyer_phone, t.item_name,
               t.seller_id, t.amount,
               t.seller_receives, t.buyer_pays,
               t.buyer_fee_share,
               u.phone_number as seller_phone
        FROM disputes d
        JOIN transactions t ON t.id = d.transaction_id
        JOIN users u ON u.id = t.seller_id
        WHERE d.id = %s
    """, (dispute_id,))

    dispute = cursor.fetchone()

    if not dispute:
        cursor.close()
        return jsonify({'error': 'Dispute not found'}), 404

    if dispute['status'] == 'RESOLVED':
        cursor.close()
        return jsonify({
            'error': 'This dispute is already resolved.'
        }), 400

    now = datetime.now()

    # Resolve the dispute
    cursor.execute("""
        UPDATE disputes SET
            status = 'RESOLVED',
            resolution = %s,
            split_buyer_amount = %s,
            split_seller_amount = %s,
            admin_notes = %s,
            resolved_by = %s,
            resolved_at = %s
        WHERE id = %s
    """, (
        data['resolution'],
        data.get('split_buyer_amount'),
        data.get('split_seller_amount'),
        data['admin_notes'].strip(),
        admin_id,
        now,
        dispute_id
    ))

    # Update transaction based on resolution
    if data['resolution'] == 'REFUND_BUYER':
        new_txn_status = 'REFUNDED'
        payout_amount = float(dispute['buyer_pays'])
        payout_phone = dispute['buyer_phone']

        # Notify buyer
        send_sms(
            dispute['buyer_phone'],
            f"Zuno: Your dispute for '{dispute['item_name']}' "
            f"has been resolved in your favor. "
            f"A full refund of Ksh {payout_amount:,.0f} "
            f"will be sent to your M-Pesa shortly."
        )
        # Notify seller
        send_sms(
            dispute['seller_phone'],
            f"Zuno: The dispute for '{dispute['item_name']}' "
            f"has been resolved. The decision was made in "
            f"the buyer's favor based on evidence reviewed. "
            f"A strike has been noted on your account."
        )

    elif data['resolution'] == 'RELEASE_TO_SELLER':
        new_txn_status = 'COMPLETED'
        payout_amount = float(dispute['seller_receives'])
        payout_phone = dispute['seller_phone']

        # Notify seller
        send_sms(
            dispute['seller_phone'],
            f"Zuno: Your dispute for '{dispute['item_name']}' "
            f"has been resolved in your favor. "
            f"Ksh {payout_amount:,.0f} will be sent to "
            f"your M-Pesa shortly."
        )
        # Notify buyer
        send_sms(
            dispute['buyer_phone'],
            f"Zuno: The dispute for '{dispute['item_name']}' "
            f"has been resolved. After reviewing evidence, "
            f"the decision was made in the seller's favor. "
            f"No refund will be issued."
        )

    else:
        # SPLIT
        new_txn_status = 'RESOLVED'
        payout_amount = float(data['split_seller_amount'])
        payout_phone = dispute['seller_phone']

        send_sms(
            dispute['buyer_phone'],
            f"Zuno: Your dispute for '{dispute['item_name']}' "
            f"has been resolved with a split decision. "
            f"You will receive Ksh "
            f"{float(data['split_buyer_amount']):,.0f} refund."
        )
        send_sms(
            dispute['seller_phone'],
            f"Zuno: Your dispute for '{dispute['item_name']}' "
            f"has been resolved with a split decision. "
            f"You will receive Ksh "
            f"{float(data['split_seller_amount']):,.0f}."
        )

    # Update transaction status
    cursor.execute("""
        UPDATE transactions SET status = %s
        WHERE id = %s
    """, (new_txn_status, dispute['transaction_id']))

    # Create payout record
    cursor.execute("""
        INSERT INTO payouts
            (seller_id, transaction_id, amount,
             mpesa_number, status)
        VALUES (%s,%s,%s,%s,'PENDING')
    """, (
        dispute['seller_id'],
        dispute['transaction_id'],
        payout_amount,
        payout_phone
    ))

    mysql.connection.commit()

    log_transaction_action(
        dispute['transaction_id'],
        action='DISPUTE_RESOLVED',
        performed_by=f"ADMIN:{admin_id}",
        old_value='DISPUTED',
        new_value=new_txn_status,
        details=f"Resolution: {data['resolution']}. "
                f"Notes: {data['admin_notes'][:100]}"
    )

    # Update seller reputation
    from app.services.trust_score import update_reputation_metrics
    update_reputation_metrics(dispute['seller_id'])

    cursor.close()

    return jsonify({
        'message': f"Dispute resolved. "
                   f"Decision: {data['resolution']}. "
                   f"Both parties have been notified.",
        'dispute_id': dispute_id,
        'resolution': data['resolution'],
        'transaction_status': new_txn_status
    }), 200


# ══════════════════════════════════════════════════════════════
# ENDPOINT 3 — PLATFORM METRICS
# ══════════════════════════════════════════════════════════════

@admin_bp.route('/metrics', methods=['GET'])
@jwt_required()
@require_admin
def get_metrics():
    """
    GET /api/admin/metrics

    Returns platform-wide statistics for the admin dashboard.
    This is what you show investors to demonstrate traction.
    """
    cursor = mysql.connection.cursor()

    # Transaction metrics
    cursor.execute("""
        SELECT
            COUNT(*) as total_transactions,
            SUM(CASE WHEN status='COMPLETED' THEN 1 ELSE 0 END)
                as completed,
            SUM(CASE WHEN status='DISPUTED' THEN 1 ELSE 0 END)
                as disputed,
            SUM(CASE WHEN status='FUNDED' THEN 1 ELSE 0 END)
                as funded,
            SUM(CASE WHEN status='PENDING' THEN 1 ELSE 0 END)
                as pending,
            SUM(CASE WHEN status='COMPLETED'
                THEN platform_fee ELSE 0 END) as total_revenue,
            SUM(CASE WHEN status IN ('FUNDED','IN_TRANSIT',
                'INSPECTION','DISPUTED')
                THEN buyer_pays ELSE 0 END) as funds_in_escrow
        FROM transactions
    """)
    txn_metrics = cursor.fetchone()

    # User metrics
    cursor.execute("""
        SELECT
            COUNT(*) as total_sellers,
            SUM(CASE WHEN is_verified=TRUE THEN 1 ELSE 0 END)
                as verified_sellers,
            SUM(CASE WHEN verification_status='PENDING'
                THEN 1 ELSE 0 END) as pending_verification
        FROM users
        WHERE role = 'seller'
    """)
    user_metrics = cursor.fetchone()

    # Dispute metrics
    cursor.execute("""
        SELECT
            COUNT(*) as total_disputes,
            SUM(CASE WHEN status='RESOLVED' THEN 1 ELSE 0 END)
                as resolved,
            SUM(CASE WHEN status='ESCALATED' THEN 1 ELSE 0 END)
                as escalated,
            SUM(CASE WHEN status NOT IN ('RESOLVED')
                THEN 1 ELSE 0 END) as open_disputes
        FROM disputes
    """)
    dispute_metrics = cursor.fetchone()

    cursor.close()

    # Format currency values
    if txn_metrics:
        txn_metrics['total_revenue'] = float(
            txn_metrics['total_revenue'] or 0
        )
        txn_metrics['funds_in_escrow'] = float(
            txn_metrics['funds_in_escrow'] or 0
        )

    return jsonify({
        'transactions': txn_metrics,
        'users': user_metrics,
        'disputes': dispute_metrics
    }), 200


# ══════════════════════════════════════════════════════════════
# ENDPOINT 4 — VERIFY SELLER
# ══════════════════════════════════════════════════════════════

@admin_bp.route('/users/<int:user_id>/verify', methods=['POST'])
@jwt_required()
@require_admin
def verify_seller(user_id):
    """
    POST /api/admin/users/<user_id>/verify

    Admin approves or rejects a seller verification request.
    Approved sellers get the Verified Badge and move to Tier 1.

    Expected body:
    {
        "decision": "APPROVED",
        "notes": "ID documents verified successfully."
    }
    """
    data = request.get_json()

    valid, error = validate_required_fields(
        data, ['decision']
    )
    if not valid:
        return jsonify({'error': error}), 400

    if data['decision'] not in ['APPROVED', 'REJECTED']:
        return jsonify({
            'error': 'Decision must be APPROVED or REJECTED'
        }), 400

    cursor = mysql.connection.cursor()
    cursor.execute(
        "SELECT * FROM users WHERE id = %s AND role = 'seller'",
        (user_id,)
    )
    user = cursor.fetchone()

    if not user:
        cursor.close()
        return jsonify({'error': 'Seller not found'}), 404

    if data['decision'] == 'APPROVED':
        cursor.execute("""
            UPDATE users SET
                verification_status = 'VERIFIED',
                is_verified = TRUE,
                seller_tier = 1,
                verified_at = %s,
                verification_notes = %s
            WHERE id = %s
        """, (datetime.now(), data.get('notes', ''), user_id))

        send_sms(
            user['phone_number'],
            f"Zuno: Congratulations {user['full_name']}! "
            f"Your account has been verified. "
            f"You now have a Verified Seller badge and "
            f"can accept deals up to Ksh 10,000. "
            f"Keep building your reputation!"
        )
    else:
        cursor.execute("""
            UPDATE users SET
                verification_status = 'REJECTED',
                verification_notes = %s
            WHERE id = %s
        """, (data.get('notes', 'Documents not accepted'), user_id))

        send_sms(
            user['phone_number'],
            f"Zuno: Your verification request was not approved. "
            f"Reason: {data.get('notes', 'Documents not accepted')}. "
            f"Please resubmit with correct documents."
        )

    mysql.connection.commit()
    cursor.close()

    return jsonify({
        'message': f"Seller verification {data['decision'].lower()}.",
        'user_id': user_id,
        'decision': data['decision']
    }), 200


# ══════════════════════════════════════════════════════════════
# ENDPOINT 5 — SUSPEND USER
# ══════════════════════════════════════════════════════════════

@admin_bp.route('/users/<int:user_id>/suspend', methods=['POST'])
@jwt_required()
@require_admin
def suspend_user(user_id):
    """
    POST /api/admin/users/<user_id>/suspend

    Suspends a user account for policy violations.

    Expected body:
    {
        "reason": "Multiple fraudulent transactions detected",
        "days": 30
    }
    """
    data = request.get_json()

    valid, error = validate_required_fields(
        data, ['reason']
    )
    if not valid:
        return jsonify({'error': error}), 400

    from datetime import timedelta
    suspend_days = int(data.get('days', 30))
    suspended_until = datetime.now() + timedelta(days=suspend_days)

    cursor = mysql.connection.cursor()
    cursor.execute(
        "SELECT * FROM users WHERE id = %s",
        (user_id,)
    )
    user = cursor.fetchone()

    if not user:
        cursor.close()
        return jsonify({'error': 'User not found'}), 404

    cursor.execute("""
        UPDATE users SET
            is_active = FALSE,
            suspension_reason = %s,
            suspended_until = %s
        WHERE id = %s
    """, (data['reason'], suspended_until, user_id))

    mysql.connection.commit()
    cursor.close()

    send_sms(
        user['phone_number'],
        f"Zuno: Your account has been suspended for "
        f"{suspend_days} days. "
        f"Reason: {data['reason']}. "
        f"Contact support@zuno.co.ke to appeal."
    )

    return jsonify({
        'message': f"User suspended for {suspend_days} days.",
        'user_id': user_id,
        'suspended_until': str(suspended_until)
    }), 200

# ══════════════════════════════════════════════════════════════
# ENDPOINT 6 — GET PENDING KYC SUBMISSIONS
# ══════════════════════════════════════════════════════════════

@admin_bp.route('/kyc/pending', methods=['GET'])
@jwt_required()
@require_admin
def get_pending_kyc():
    """
    GET /api/admin/kyc/pending

    Returns all KYC submissions waiting for admin review.
    Admin works through this queue to verify sellers.
    Sorted oldest first — first in, first out.
    """
    cursor = mysql.connection.cursor()

    cursor.execute("""
        SELECT
            k.id,
            k.seller_id,
            k.full_name,
            k.national_id_number,
            k.date_of_birth,
            k.id_front_url,
            k.id_back_url,
            k.selfie_url,
            k.business_name,
            k.business_type,
            k.kra_pin,
            k.status,
            k.submitted_at,
            u.phone_number,
            u.email,
            u.seller_tier,
            u.verification_status
        FROM kyc_submissions k
        JOIN users u ON u.id = k.seller_id
        WHERE k.status IN ('SUBMITTED', 'UNDER_REVIEW', 'RESUBMITTED')
        ORDER BY k.submitted_at ASC
    """)

    submissions = cursor.fetchall()
    cursor.close()

    for sub in submissions:
        if sub.get('submitted_at'):
            sub['submitted_at'] = str(sub['submitted_at'])
        if sub.get('date_of_birth'):
            sub['date_of_birth'] = str(sub['date_of_birth'])

    return jsonify({
        'pending_kyc': submissions,
        'total': len(submissions)
    }), 200


# ══════════════════════════════════════════════════════════════
# ENDPOINT 7 — REVIEW KYC SUBMISSION
# ══════════════════════════════════════════════════════════════

@admin_bp.route('/kyc/<int:submission_id>/review', methods=['POST'])
@jwt_required()
@require_admin
def review_kyc(submission_id):
    """
    POST /api/admin/kyc/<submission_id>/review

    Admin approves or rejects a KYC submission.

    On APPROVED:
    - Seller gets Verified Badge
    - Seller tier upgrades to 1
    - Transaction limit increases to Ksh 10,000
    - Seller can list products in catalog
    - SMS notification sent to seller

    On REJECTED:
    - Clear reason given so seller knows what to fix
    - Seller can resubmit with corrected documents
    - SMS notification sent to seller

    Expected body:
    {
        "decision": "APPROVED",
        "review_notes": "Documents verified. National ID matches selfie.",
        "rejection_reason": null
    }

    For rejection:
    {
        "decision": "REJECTED",
        "review_notes": "ID photo is blurry and unreadable.",
        "rejection_reason": "Please resubmit a clear, well-lit photo of your National ID."
    }
    """
    admin_id = get_jwt_identity()
    data = request.get_json()

    valid, error = validate_required_fields(
        data, ['decision', 'review_notes']
    )
    if not valid:
        return jsonify({'error': error}), 400

    if data['decision'] not in ['APPROVED', 'REJECTED']:
        return jsonify({
            'error': 'Decision must be APPROVED or REJECTED'
        }), 400

    # Rejection requires a reason
    if data['decision'] == 'REJECTED' and \
       not data.get('rejection_reason'):
        return jsonify({
            'error': 'rejection_reason is required when rejecting. '
                     'The seller needs to know what to fix.'
        }), 400

    cursor = mysql.connection.cursor()

    # Get the KYC submission
    cursor.execute("""
        SELECT k.*, u.phone_number, u.full_name
        FROM kyc_submissions k
        JOIN users u ON u.id = k.seller_id
        WHERE k.id = %s
    """, (submission_id,))

    submission = cursor.fetchone()

    if not submission:
        cursor.close()
        return jsonify({'error': 'KYC submission not found'}), 404

    now = datetime.now()

    if data['decision'] == 'APPROVED':
        # Update KYC submission
        cursor.execute("""
            UPDATE kyc_submissions SET
                status = 'APPROVED',
                reviewed_by = %s,
                review_notes = %s,
                reviewed_at = %s,
                approved_at = %s
            WHERE id = %s
        """, (
            admin_id,
            data['review_notes'],
            now, now,
            submission_id
        ))

        # Update user — award verified badge and increase tier
        cursor.execute("""
            UPDATE users SET
                verification_status = 'VERIFIED',
                is_verified = TRUE,
                seller_tier = 1,
                verified_at = %s,
                verification_notes = %s
            WHERE id = %s
        """, (
            now,
            data['review_notes'],
            submission['seller_id']
        ))

        mysql.connection.commit()
        cursor.close()

        # Notify seller of approval
        from app.services.sms import send_sms
        send_sms(
            submission['phone_number'],
            f"Zuno: Congratulations {submission['full_name']}! "
            f"Your KYC verification has been approved. "
            f"You now have a Verified Seller badge. "
            f"Your transaction limit is now Ksh 10,000. "
            f"You can now list products in the Zuno catalog. "
            f"Keep building your reputation!"
        )

        return jsonify({
            'message': f"KYC approved for {submission['full_name']}. "
                       f"Seller now has Verified Badge and Tier 1 access.",
            'submission_id': submission_id,
            'seller_id': submission['seller_id'],
            'decision': 'APPROVED'
        }), 200

    else:
        # REJECTED
        cursor.execute("""
            UPDATE kyc_submissions SET
                status = 'REJECTED',
                reviewed_by = %s,
                review_notes = %s,
                rejection_reason = %s,
                reviewed_at = %s
            WHERE id = %s
        """, (
            admin_id,
            data['review_notes'],
            data['rejection_reason'],
            now,
            submission_id
        ))

        # Update user status back to allow resubmission
        cursor.execute("""
            UPDATE users SET
                verification_status = 'REJECTED',
                verification_notes = %s
            WHERE id = %s
        """, (data['rejection_reason'], submission['seller_id']))

        mysql.connection.commit()
        cursor.close()

        # Notify seller of rejection with clear reason
        from app.services.sms import send_sms
        send_sms(
            submission['phone_number'],
            f"Zuno: Your verification was not approved. "
            f"Reason: {data['rejection_reason']} "
            f"Please resubmit with corrected documents at "
            f"POST /api/auth/kyc/submit"
        )

        return jsonify({
            'message': f"KYC rejected for {submission['full_name']}. "
                       f"Seller has been notified with reason.",
            'submission_id': submission_id,
            'decision': 'REJECTED',
            'rejection_reason': data['rejection_reason']
        }), 200