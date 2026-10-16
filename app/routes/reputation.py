# app/routes/reputation.py
#
# Reputation and Ratings System for Zuno.
#
# This is what makes Zuno a trusted marketplace.
# Every seller has a public reputation profile that
# buyers can view before deciding to transact.
#
# HOW THE STAR RATING WORKS (like Airbnb):
# - After every completed transaction, buyer rates seller 1-5 stars
# - Each new rating updates the running average automatically
# - Average is displayed as e.g. "4.8 ★" on seller profile
# - Minimum 3 ratings before stars are publicly displayed
#   (prevents gaming with 1-2 fake reviews)
#
# HOW THE TRUST SCORE WORKS:
# - Composite score from 0-100 based on 5 metrics
# - Completion rate (35%), dispute rate (25%),
#   average rating (20%), cancellation rate (10%),
#   late delivery rate (10%)
# - Displayed as levels: NEW, BRONZE, SILVER, GOLD, PLATINUM
#
# ENDPOINTS:
# GET  /api/reputation/seller/<id>          → public seller profile
# GET  /api/reputation/seller/<id>/ratings  → all ratings for seller
# POST /api/reputation/rate                 → buyer rates a transaction
# GET  /api/reputation/leaderboard          → top sellers on platform

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app import mysql
from app.utils.validators import validate_required_fields
from app.services.trust_score import update_reputation_metrics
from datetime import datetime

reputation_bp = Blueprint('reputation', __name__)


# ══════════════════════════════════════════════════════════════
# ENDPOINT 1 — GET SELLER PUBLIC REPUTATION PROFILE
# ══════════════════════════════════════════════════════════════

@reputation_bp.route('/seller/<int:seller_id>', methods=['GET'])
def get_seller_reputation(seller_id):
    """
    GET /api/reputation/seller/<seller_id>

    Returns a seller's complete public reputation profile.
    This is what buyers see before deciding to transact.

    Displayed on:
    - Seller's public profile page
    - Payment link page (so buyer can vet seller before paying)
    - Search results

    No login required — this is public information.
    """
    cursor = mysql.connection.cursor()

    cursor.execute("""
        SELECT
            u.id,
            u.full_name,
            u.business_name,
            u.bio,
            u.profile_photo,
            u.is_verified,
            u.verification_status,
            u.seller_tier,
            u.created_at,
            r.total_transactions,
            r.completed_transactions,
            r.cancelled_transactions,
            r.disputed_transactions,
            r.completion_rate,
            r.cancellation_rate,
            r.dispute_rate,
            r.late_delivery_rate,
            r.avg_response_time_minutes,
            r.total_ratings,
            r.average_rating,
            r.trust_score,
            r.trust_level,
            r.is_visible_in_search,
            r.strike_count
        FROM users u
        LEFT JOIN seller_reputation r ON r.seller_id = u.id
        WHERE u.id = %s
        AND u.role = 'seller'
        AND u.is_active = TRUE
    """, (seller_id,))

    seller = cursor.fetchone()

    if not seller:
        cursor.close()
        return jsonify({'error': 'Seller not found'}), 404

    # Convert datetime to string
    if seller.get('created_at'):
        seller['created_at'] = str(seller['created_at'])

    # Build the star rating display
    # Only show stars if seller has at least 3 ratings
    # This prevents gaming with 1-2 fake reviews
    rating_display = None
    if seller['total_ratings'] and seller['total_ratings'] >= 3:
        avg = float(seller['average_rating'] or 0)
        rating_display = {
            'stars': avg,
            'display': f"{avg:.2f} ★",
            'total_reviews': seller['total_ratings'],
            'breakdown': _get_rating_breakdown(cursor, seller_id)
        }
    elif seller['total_ratings'] and seller['total_ratings'] > 0:
        rating_display = {
            'stars': None,
            'display': 'New seller — building reputation',
            'total_reviews': seller['total_ratings'],
            'note': f"{seller['total_ratings']} rating(s) so far. "
                    f"Displayed after 3 reviews."
        }
    else:
        rating_display = {
            'stars': None,
            'display': 'No ratings yet',
            'total_reviews': 0
        }

    # Trust level badge display
    trust_badges = {
        'NEW': {'label': 'New Seller', 'color': '#gray'},
        'BRONZE': {'label': 'Bronze Seller', 'color': '#CD7F32'},
        'SILVER': {'label': 'Silver Seller', 'color': '#C0C0C0'},
        'GOLD': {'label': 'Gold Seller', 'color': '#FFD700'},
        'PLATINUM': {'label': 'Platinum Seller', 'color': '#E5E4E2'}
    }

    trust_level = seller.get('trust_level', 'NEW')
    badge = trust_badges.get(trust_level, trust_badges['NEW'])

    cursor.close()

    return jsonify({
        'seller': {
            'id': seller['id'],
            'name': seller['business_name'] or seller['full_name'],
            'bio': seller['bio'],
            'profile_photo': seller['profile_photo'],
            'is_verified': seller['is_verified'],
            'member_since': str(seller['created_at'])[:10],

            # Star rating (Airbnb style)
            'rating': rating_display,

            # Trust score and badge
            'trust_score': seller['trust_score'],
            'trust_level': trust_level,
            'trust_badge': badge,

            # Performance metrics
            'metrics': {
                'total_transactions': seller['total_transactions'] or 0,
                'completion_rate': f"{seller['completion_rate'] or 0:.1f}%",
                'cancellation_rate': f"{seller['cancellation_rate'] or 0:.1f}%",
                'dispute_rate': f"{seller['dispute_rate'] or 0:.1f}%",
                'late_delivery_rate': f"{seller['late_delivery_rate'] or 0:.1f}%"
            },

            # Visibility status
            'is_visible_in_search': seller['is_visible_in_search']
        }
    }), 200


# ══════════════════════════════════════════════════════════════
# ENDPOINT 2 — GET ALL RATINGS FOR A SELLER
# ══════════════════════════════════════════════════════════════

@reputation_bp.route('/seller/<int:seller_id>/ratings',
                     methods=['GET'])
def get_seller_ratings(seller_id):
    """
    GET /api/reputation/seller/<seller_id>/ratings

    Returns all individual ratings and comments for a seller.
    Displayed on seller profile like Airbnb reviews section.

    Shows:
    - Star rating (1-5)
    - Written comment
    - Which item it was for
    - Date of review
    - Masked buyer phone (privacy)

    No login required — public information.
    """
    # Optional pagination
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 10))
    offset = (page - 1) * per_page

    cursor = mysql.connection.cursor()

    # Get ratings with transaction details
    cursor.execute("""
        SELECT
            r.id,
            r.score,
            r.comment,
            r.created_at,
            r.buyer_phone,
            t.item_name
        FROM ratings r
        JOIN transactions t ON t.id = r.transaction_id
        WHERE r.seller_id = %s
        ORDER BY r.created_at DESC
        LIMIT %s OFFSET %s
    """, (seller_id, per_page, offset))

    ratings = cursor.fetchall()

    # Get total count for pagination
    cursor.execute("""
        SELECT COUNT(*) as total
        FROM ratings
        WHERE seller_id = %s
    """, (seller_id,))

    total = cursor.fetchone()['total']

    # Get rating breakdown (how many 5★, 4★, 3★ etc.)
    cursor.execute("""
        SELECT
            score,
            COUNT(*) as count
        FROM ratings
        WHERE seller_id = %s
        GROUP BY score
        ORDER BY score DESC
    """, (seller_id,))

    breakdown_raw = cursor.fetchall()
    cursor.close()

    # Build breakdown like Airbnb
    # e.g. {5: 45, 4: 12, 3: 3, 2: 1, 1: 0}
    breakdown = {5: 0, 4: 0, 3: 0, 2: 0, 1: 0}
    for row in breakdown_raw:
        breakdown[row['score']] = row['count']

    # Mask buyer phone for privacy
    # Show only first 4 and last 2 digits
    # e.g. 0712****32
    for rating in ratings:
        phone = rating['buyer_phone']
        if phone and len(phone) >= 6:
            rating['buyer_phone'] = (
                phone[:4] + '****' + phone[-2:]
            )
        if rating.get('created_at'):
            rating['created_at'] = str(rating['created_at'])

        # Add star display
        rating['stars_display'] = '★' * rating['score'] + \
                                   '☆' * (5 - rating['score'])

    return jsonify({
        'seller_id': seller_id,
        'ratings': ratings,
        'breakdown': {
            '5_stars': breakdown[5],
            '4_stars': breakdown[4],
            '3_stars': breakdown[3],
            '2_stars': breakdown[2],
            '1_star': breakdown[1]
        },
        'pagination': {
            'page': page,
            'per_page': per_page,
            'total': total,
            'total_pages': (total + per_page - 1) // per_page
        }
    }), 200


# ══════════════════════════════════════════════════════════════
# ENDPOINT 3 — BUYER RATES A TRANSACTION
# ══════════════════════════════════════════════════════════════

@reputation_bp.route('/rate', methods=['POST'])
def rate_transaction():
    """
    POST /api/reputation/rate

    Buyer rates a seller after a completed transaction.

    Rules:
    - Only the buyer of that transaction can rate
    - Transaction must be COMPLETED
    - Each transaction can only be rated once
    - Score must be between 1 and 5
    - Comment is optional but encouraged
    """
    data = request.get_json()

    valid, error = validate_required_fields(data, [
        'transaction_id', 'buyer_phone', 'score'
    ])
    if not valid:
        return jsonify({'error': error}), 400

    # Validate score range
    try:
        score = int(data['score'])
        if score < 1 or score > 5:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({
            'error': 'Score must be a whole number between 1 and 5'
        }), 400

    cursor = mysql.connection.cursor()

    # Verify transaction exists and is completed
    cursor.execute(
        "SELECT * FROM transactions WHERE id = %s",
        (data['transaction_id'],)
    )
    txn = cursor.fetchone()

    if not txn:
        cursor.close()
        return jsonify({'error': 'Transaction not found'}), 404

    # Verify buyer is correct
    if txn['buyer_phone'] != data['buyer_phone']:
        cursor.close()
        return jsonify({
            'error': 'Phone number does not match this transaction'
        }), 403

    # Can only rate completed transactions
    if txn['status'] not in ['COMPLETED', 'RESOLVED']:
        cursor.close()
        return jsonify({
            'error': f"Cannot rate this transaction. "
                     f"Status is '{txn['status']}'. "
                     f"Ratings are only for completed transactions."
        }), 400

    # Check if already rated
    cursor.execute("""
        SELECT id FROM ratings
        WHERE transaction_id = %s
    """, (data['transaction_id'],))

    if cursor.fetchone():
        cursor.close()
        return jsonify({
            'error': 'You have already rated this transaction. '
                     'Each transaction can only be rated once.'
        }), 409

    # Save the rating
    cursor.execute("""
        INSERT INTO ratings
            (transaction_id, seller_id,
             buyer_phone, score, comment)
        VALUES (%s, %s, %s, %s, %s)
    """, (
        data['transaction_id'],
        txn['seller_id'],
        data['buyer_phone'],
        score,
        data.get('comment', '').strip()
    ))

    # Recalculate average rating from scratch to ensure accuracy
    cursor.execute("""
        SELECT
            COUNT(*) as total,
            AVG(score) as average
        FROM ratings
        WHERE seller_id = %s
    """, (txn['seller_id'],))

    rating_stats = cursor.fetchone()

    cursor.execute("""
        UPDATE seller_reputation SET
            total_ratings = %s,
            average_rating = %s
        WHERE seller_id = %s
    """, (
        rating_stats['total'],
        round(float(rating_stats['average']), 2),
        txn['seller_id']
    ))

    # ── THIS IS WHAT WAS MISSING ──────────────────────────────
    # Commit saves everything to the database
    mysql.connection.commit()
    cursor.close()

    # Recalculate trust score with new rating
    try:
        update_reputation_metrics(txn['seller_id'])
    except Exception as e:
        # Log error but do not crash — rating is already saved
        print(f"[REPUTATION] Metrics update failed: {str(e)}")

    # Build star display for response
    stars_display = '★' * score + '☆' * (5 - score)

    # ── THIS RETURN WAS MISSING — CAUSED THE CRASH ────────────
    return jsonify({
        'message': 'Rating submitted successfully. Thank you!',
        'transaction_id': data['transaction_id'],
        'score': score,
        'stars': stars_display,
        'comment': data.get('comment', '')
    }), 201

# ══════════════════════════════════════════════════════════════
# ENDPOINT 4 — PLATFORM LEADERBOARD
# ══════════════════════════════════════════════════════════════

@reputation_bp.route('/leaderboard', methods=['GET'])
def get_leaderboard():
    """
    GET /api/reputation/leaderboard
    GET /api/reputation/leaderboard?limit=10

    Returns top sellers on the platform ranked by trust score.
    Only shows sellers who are visible in search.

    Like Airbnb's Superhost section — showcases the most
    trusted sellers and encourages others to build reputation.
    """
    limit = int(request.args.get('limit', 10))
    limit = min(limit, 50)  # cap at 50 to prevent abuse

    cursor = mysql.connection.cursor()

    cursor.execute("""
        SELECT
            u.id,
            u.full_name,
            u.business_name,
            u.profile_photo,
            u.is_verified,
            r.trust_score,
            r.trust_level,
            r.total_transactions,
            r.completion_rate,
            r.average_rating,
            r.total_ratings
        FROM seller_reputation r
        JOIN users u ON u.id = r.seller_id
        WHERE r.is_visible_in_search = TRUE
        AND u.is_active = TRUE
        AND r.total_transactions >= 1
        ORDER BY r.trust_score DESC, r.total_transactions DESC
        LIMIT %s
    """, (limit,))

    sellers = cursor.fetchall()
    cursor.close()

    leaderboard = []
    for rank, seller in enumerate(sellers, 1):
        avg_rating = float(seller['average_rating'] or 0)
        leaderboard.append({
            'rank': rank,
            'seller_id': seller['id'],
            'name': seller['business_name'] or seller['full_name'],
            'profile_photo': seller['profile_photo'],
            'is_verified': seller['is_verified'],
            'trust_score': seller['trust_score'],
            'trust_level': seller['trust_level'],
            'total_transactions': seller['total_transactions'],
            'completion_rate': f"{seller['completion_rate'] or 0:.1f}%",
            'rating': (
                f"{avg_rating:.2f} ★"
                if seller['total_ratings'] and
                seller['total_ratings'] >= 3
                else 'New'
            ),
            'total_reviews': seller['total_ratings'] or 0
        })

    return jsonify({
        'leaderboard': leaderboard,
        'total': len(leaderboard)
    }), 200


# ══════════════════════════════════════════════════════════════
# PRIVATE HELPER
# ══════════════════════════════════════════════════════════════

def _get_rating_breakdown(cursor, seller_id):
    """
    Returns count of each star rating for a seller.
    Used internally to build the rating breakdown display.
    """
    cursor.execute("""
        SELECT score, COUNT(*) as count
        FROM ratings
        WHERE seller_id = %s
        GROUP BY score
    """, (seller_id,))

    rows = cursor.fetchall()
    breakdown = {5: 0, 4: 0, 3: 0, 2: 0, 1: 0}

    for row in rows:
        breakdown[row['score']] = row['count']

    total = sum(breakdown.values())

    percentages = {}
    for star, count in breakdown.items():
        percentages[f"{star}_stars"] = (
            round((count / total) * 100, 1) if total > 0 else 0
        )

    return {
        '5_stars': breakdown[5],
        '4_stars': breakdown[4],
        '3_stars': breakdown[3],
        '2_stars': breakdown[2],
        '1_star': breakdown[1],
        'percentages': percentages
    }