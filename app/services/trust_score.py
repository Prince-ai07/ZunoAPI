# app/services/trust_score.py
#
# Calculates the Zuno Trust Score for every seller.
# Called automatically after every transaction event.
#
# SCORE WEIGHTS:
# Completion rate   → 35 points  (most important)
# Dispute rate      → 25 points  (penalty)
# Average rating    → 20 points
# Cancellation rate → 10 points  (penalty)
# Late delivery     → 10 points  (penalty)
# TOTAL             → 100 points maximum

from app import mysql

WEIGHTS = {
    'completion_rate':    35,
    'dispute_rate':       25,
    'average_rating':     20,
    'cancellation_rate':  10,
    'late_delivery_rate': 10,
}


def calculate_trust_score(seller_id):
    """
    Calculates and saves the composite Trust Score for a seller.
    Score ranges from 0 to 100.
    Requires at least 3 transactions before scoring.
    """
    cursor = mysql.connection.cursor()

    cursor.execute("""
        SELECT completion_rate, dispute_rate, average_rating,
               cancellation_rate, late_delivery_rate,
               total_transactions
        FROM seller_reputation
        WHERE seller_id = %s
    """, (seller_id,))

    rep = cursor.fetchone()

    if not rep or rep['total_transactions'] < 3:
        cursor.close()
        return

    # Positive contributors
    completion_score = (rep['completion_rate'] / 100) * WEIGHTS['completion_rate']
    rating_score = ((rep['average_rating'] / 5) * 100 / 100) * WEIGHTS['average_rating']

    # Penalties (higher rate = more points deducted)
    dispute_penalty = (rep['dispute_rate'] / 100) * WEIGHTS['dispute_rate']
    cancel_penalty = (rep['cancellation_rate'] / 100) * WEIGHTS['cancellation_rate']
    late_penalty = (rep['late_delivery_rate'] / 100) * WEIGHTS['late_delivery_rate']

    # Final score clamped between 0 and 100
    raw = completion_score + rating_score - dispute_penalty - cancel_penalty - late_penalty
    trust_score = max(0.0, min(100.0, round(raw, 2)))

    # Assign trust level badge
    if trust_score <= 20:
        trust_level = 'NEW'
    elif trust_score <= 40:
        trust_level = 'BRONZE'
    elif trust_score <= 60:
        trust_level = 'SILVER'
    elif trust_score <= 80:
        trust_level = 'GOLD'
    else:
        trust_level = 'PLATINUM'

    # Visibility penalty for high dispute rates
    is_visible = True
    penalty_reason = None
    if rep['dispute_rate'] > 20 and rep['total_transactions'] >= 5:
        is_visible = False
        penalty_reason = (
            f"Dispute rate of {rep['dispute_rate']}% exceeds 20% threshold."
        )

    cursor.execute("""
        UPDATE seller_reputation SET
            trust_score = %s,
            trust_level = %s,
            is_visible_in_search = %s,
            visibility_penalty_reason = %s
        WHERE seller_id = %s
    """, (trust_score, trust_level, is_visible, penalty_reason, seller_id))

    mysql.connection.commit()
    cursor.close()
    return trust_score


def update_reputation_metrics(seller_id):
    """
    Recalculates ALL reputation metrics from raw data.
    Call this after every transaction status change.
    """
    cursor = mysql.connection.cursor()

    cursor.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN status='COMPLETED' THEN 1 ELSE 0 END) as completed,
            SUM(CASE WHEN status='CANCELLED' THEN 1 ELSE 0 END) as cancelled,
            SUM(CASE WHEN status IN ('DISPUTED','RESOLVED','REFUNDED')
                THEN 1 ELSE 0 END) as disputed
        FROM transactions
        WHERE seller_id = %s AND status != 'PENDING'
    """, (seller_id,))

    stats = cursor.fetchone()
    total = stats['total'] or 0

    if total == 0:
        cursor.close()
        return

    completion_rate = round((stats['completed'] / total) * 100, 2)
    cancellation_rate = round((stats['cancelled'] / total) * 100, 2)
    dispute_rate = round((stats['disputed'] / total) * 100, 2)

    cursor.execute("""
        UPDATE seller_reputation SET
            total_transactions = %s,
            completed_transactions = %s,
            cancelled_transactions = %s,
            disputed_transactions = %s,
            completion_rate = %s,
            cancellation_rate = %s,
            dispute_rate = %s
        WHERE seller_id = %s
    """, (
        total, stats['completed'], stats['cancelled'],
        stats['disputed'], completion_rate,
        cancellation_rate, dispute_rate, seller_id
    ))

    mysql.connection.commit()
    cursor.close()

    calculate_trust_score(seller_id)