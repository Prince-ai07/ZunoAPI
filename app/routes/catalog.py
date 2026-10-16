# app/routes/catalog.py
#
# Product Catalog for Verified Sellers on Zuno.
#
# This gives verified sellers a proper storefront
# where they can list their products with photos,
# descriptions and prices.
#
# KEY RULE:
# Only VERIFIED sellers can list products.
# Only VERIFIED sellers can add external links.
# Unverified sellers see an upgrade prompt instead.
#
# ENDPOINTS:
# POST /api/catalog/add              → add a product
# GET  /api/catalog/seller/<id>      → view seller's products
# GET  /api/catalog/search           → search all products
# PUT  /api/catalog/<id>/update      → update a product
# DELETE /api/catalog/<id>           → remove a product
# GET  /api/catalog/<id>             → get single product

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app import mysql
from app.utils.validators import validate_required_fields, validate_amount

catalog_bp = Blueprint('catalog', __name__)


# ══════════════════════════════════════════════════════════════
# ENDPOINT 1 — ADD PRODUCT
# ══════════════════════════════════════════════════════════════

@catalog_bp.route('/add', methods=['POST'])
@jwt_required()
def add_product():
    """
    POST /api/catalog/add

    Verified seller adds a product to their catalog.
    Only verified sellers can do this.

    Expected body:
    {
        "name": "Nike Air Max 2026",
        "description": "Brand new Nike Air Max, available in sizes 39-45",
        "price": 8500,
        "category": "Footwear",
        "photos": ["https://cloudinary.com/photo1.jpg"],
        "external_link": "https://instagram.com/p/xxx"  (verified only)
    }
    """
    user_id = get_jwt_identity()
    data = request.get_json()

    valid, error = validate_required_fields(
        data, ['name', 'price']
    )
    if not valid:
        return jsonify({'error': error}), 400

    valid, error = validate_amount(data['price'])
    if not valid:
        return jsonify({'error': error}), 400

    cursor = mysql.connection.cursor()

    # Get seller details
    cursor.execute("""
        SELECT id, is_verified, verification_status
        FROM users
        WHERE id = %s AND role = 'seller'
    """, (user_id,))

    seller = cursor.fetchone()

    if not seller:
        cursor.close()
        return jsonify({'error': 'Seller account not found'}), 404

    # Only verified sellers can list products
    if not seller['is_verified']:
        cursor.close()
        return jsonify({
            'error': 'Only verified sellers can list products. '
                     'Please apply for verification first.',
            'verification_status': seller['verification_status'],
            'next_step': 'POST /api/auth/apply-verification'
        }), 403

    # Handle external link
    # Only verified sellers can add links
    # and links must not be promotional spam
    external_link = None
    if data.get('external_link'):
        external_link = data['external_link'].strip()

    # Handle photos array
    # Store as comma-separated string in database
    photos = None
    if data.get('photos') and isinstance(data['photos'], list):
        photos = ','.join(data['photos'][:5])  # max 5 photos

    cursor.execute("""
        INSERT INTO product_catalog (
            seller_id, name, description,
            price, category, photos, external_link
        ) VALUES (%s,%s,%s,%s,%s,%s,%s)
    """, (
        user_id,
        data['name'].strip(),
        data.get('description', '').strip(),
        float(data['price']),
        data.get('category', 'General').strip(),
        photos,
        external_link
    ))

    product_id = cursor.lastrowid
    mysql.connection.commit()
    cursor.close()

    return jsonify({
        'message': 'Product listed successfully.',
        'product_id': product_id,
        'note': 'Buyers can now find your product '
                'and initiate a deal directly.'
    }), 201


# ══════════════════════════════════════════════════════════════
# ENDPOINT 2 — GET SELLER CATALOG
# ══════════════════════════════════════════════════════════════

@catalog_bp.route('/seller/<int:seller_id>', methods=['GET'])
def get_seller_catalog(seller_id):
    """
    GET /api/catalog/seller/<seller_id>

    Returns all products listed by a specific seller.
    This is the seller's storefront — what buyers browse.
    No login required — public information.
    """
    cursor = mysql.connection.cursor()

    # Get seller info
    cursor.execute("""
        SELECT full_name, business_name,
               is_verified, profile_photo
        FROM users
        WHERE id = %s AND is_active = TRUE
    """, (seller_id,))

    seller = cursor.fetchone()

    if not seller:
        cursor.close()
        return jsonify({'error': 'Seller not found'}), 404

    # Get all available products
    cursor.execute("""
        SELECT
            id, name, description, price,
            category, photos, external_link,
            is_available, created_at
        FROM product_catalog
        WHERE seller_id = %s
        AND is_available = TRUE
        ORDER BY created_at DESC
    """, (seller_id,))

    products = cursor.fetchall()
    cursor.close()

    # Format products
    for product in products:
        # Convert photos string back to array
        if product.get('photos'):
            product['photos'] = product['photos'].split(',')
        else:
            product['photos'] = []

        if product.get('created_at'):
            product['created_at'] = str(product['created_at'])

        # Format price
        product['price_display'] = f"Ksh {float(product['price']):,.0f}"

    return jsonify({
        'seller': {
            'id': seller_id,
            'name': seller['business_name'] or seller['full_name'],
            'is_verified': seller['is_verified'],
            'profile_photo': seller['profile_photo']
        },
        'products': products,
        'total': len(products)
    }), 200


# ══════════════════════════════════════════════════════════════
# ENDPOINT 3 — SEARCH PRODUCTS
# ══════════════════════════════════════════════════════════════

@catalog_bp.route('/search', methods=['GET'])
def search_products():
    """
    GET /api/catalog/search?q=nike&category=footwear&min=1000&max=10000

    Searches all products across all verified sellers.
    Only shows products from sellers visible in search.

    Query parameters:
        q        → search term (searches name and description)
        category → filter by category
        min      → minimum price
        max      → maximum price
        page     → page number (default 1)
    """
    query = request.args.get('q', '').strip()
    category = request.args.get('category', '').strip()
    min_price = request.args.get('min', 0)
    max_price = request.args.get('max', 10000000)
    page = int(request.args.get('page', 1))
    per_page = 20
    offset = (page - 1) * per_page

    cursor = mysql.connection.cursor()

    # Build search query dynamically
    # Only show products from verified sellers
    # who are visible in search
    search_query = """
        SELECT
            p.id, p.name, p.description,
            p.price, p.category, p.photos,
            p.external_link,
            u.id as seller_id,
            u.full_name as seller_name,
            u.business_name,
            u.is_verified,
            r.trust_score,
            r.trust_level,
            r.average_rating,
            r.total_ratings
        FROM product_catalog p
        JOIN users u ON u.id = p.seller_id
        LEFT JOIN seller_reputation r ON r.seller_id = u.id
        WHERE p.is_available = TRUE
        AND u.is_active = TRUE
        AND u.is_verified = TRUE
        AND (r.is_visible_in_search = TRUE OR r.seller_id IS NULL)
        AND p.price BETWEEN %s AND %s
    """

    params = [float(min_price), float(max_price)]

    # Add search term filter
    if query:
        search_query += """
            AND (p.name LIKE %s OR p.description LIKE %s)
        """
        params.extend([f"%{query}%", f"%{query}%"])

    # Add category filter
    if category:
        search_query += " AND p.category = %s"
        params.append(category)

    # Sort by trust score — most trusted sellers first
    search_query += """
        ORDER BY r.trust_score DESC, p.created_at DESC
        LIMIT %s OFFSET %s
    """
    params.extend([per_page, offset])

    cursor.execute(search_query, params)
    products = cursor.fetchall()
    cursor.close()

    # Format results
    for product in products:
        if product.get('photos'):
            product['photos'] = product['photos'].split(',')
        else:
            product['photos'] = []

        product['price_display'] = f"Ksh {float(product['price']):,.0f}"

        avg = float(product['average_rating'] or 0)
        total = product['total_ratings'] or 0
        product['rating_display'] = (
            f"{avg:.1f} ★ ({total} reviews)"
            if total >= 3 else 'New seller'
        )

    return jsonify({
        'results': products,
        'total': len(products),
        'query': query,
        'page': page
    }), 200


# ══════════════════════════════════════════════════════════════
# ENDPOINT 4 — UPDATE PRODUCT
# ══════════════════════════════════════════════════════════════

@catalog_bp.route('/<int:product_id>/update', methods=['PUT'])
@jwt_required()
def update_product(product_id):
    """
    PUT /api/catalog/<product_id>/update

    Seller updates their product details.
    Can only update their own products.
    """
    user_id = get_jwt_identity()
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    cursor = mysql.connection.cursor()

    # Verify product belongs to this seller
    cursor.execute("""
        SELECT id FROM product_catalog
        WHERE id = %s AND seller_id = %s
    """, (product_id, user_id))

    if not cursor.fetchone():
        cursor.close()
        return jsonify({
            'error': 'Product not found'
        }), 404

    # Build update dynamically
    allowed = ['name', 'description', 'price',
               'category', 'is_available']
    updates = {}

    for field in allowed:
        if field in data:
            updates[field] = data[field]

    # Handle photos separately
    if 'photos' in data and isinstance(data['photos'], list):
        updates['photos'] = ','.join(data['photos'][:5])

    if not updates:
        cursor.close()
        return jsonify({'error': 'No valid fields to update'}), 400

    set_clause = ', '.join([f"{f} = %s" for f in updates.keys()])
    values = list(updates.values()) + [product_id]

    cursor.execute(
        f"UPDATE product_catalog SET {set_clause} WHERE id = %s",
        values
    )
    mysql.connection.commit()
    cursor.close()

    return jsonify({
        'message': 'Product updated successfully.',
        'product_id': product_id
    }), 200


# ══════════════════════════════════════════════════════════════
# ENDPOINT 5 — DELETE PRODUCT
# ══════════════════════════════════════════════════════════════

@catalog_bp.route('/<int:product_id>', methods=['DELETE'])
@jwt_required()
def delete_product(product_id):
    """
    DELETE /api/catalog/<product_id>

    Removes a product from the seller's catalog.
    We soft delete — mark as unavailable, not actually delete.
    This preserves data integrity for past transactions.
    """
    user_id = get_jwt_identity()

    cursor = mysql.connection.cursor()

    cursor.execute("""
        SELECT id FROM product_catalog
        WHERE id = %s AND seller_id = %s
    """, (product_id, user_id))

    if not cursor.fetchone():
        cursor.close()
        return jsonify({'error': 'Product not found'}), 404

    # Soft delete — mark unavailable
    cursor.execute("""
        UPDATE product_catalog
        SET is_available = FALSE
        WHERE id = %s
    """, (product_id,))

    mysql.connection.commit()
    cursor.close()

    return jsonify({
        'message': 'Product removed from catalog.'
    }), 200


# ══════════════════════════════════════════════════════════════
# ENDPOINT 6 — GET SINGLE PRODUCT
# ══════════════════════════════════════════════════════════════

@catalog_bp.route('/<int:product_id>', methods=['GET'])
def get_product(product_id):
    """
    GET /api/catalog/<product_id>

    Returns details of a single product.
    Used when a buyer clicks on a product listing.
    Shows seller reputation alongside product details
    so buyer can make an informed decision.
    """
    cursor = mysql.connection.cursor()

    cursor.execute("""
        SELECT
            p.*,
            u.full_name as seller_name,
            u.business_name,
            u.is_verified,
            u.profile_photo as seller_photo,
            r.trust_score,
            r.trust_level,
            r.average_rating,
            r.total_ratings,
            r.completion_rate,
            r.total_transactions
        FROM product_catalog p
        JOIN users u ON u.id = p.seller_id
        LEFT JOIN seller_reputation r ON r.seller_id = u.id
        WHERE p.id = %s
        AND p.is_available = TRUE
    """, (product_id,))

    product = cursor.fetchone()
    cursor.close()

    if not product:
        return jsonify({'error': 'Product not found'}), 404

    # Format photos
    if product.get('photos'):
        product['photos'] = product['photos'].split(',')
    else:
        product['photos'] = []

    for field in ['created_at', 'updated_at']:
        if product.get(field):
            product[field] = str(product[field])

    product['price_display'] = f"Ksh {float(product['price']):,.0f}"

    avg = float(product['average_rating'] or 0)
    total = product['total_ratings'] or 0
    product['seller_rating'] = (
        f"{avg:.2f} ★ ({total} reviews)"
        if total >= 3 else 'Building reputation'
    )

    return jsonify({'product': product}), 200