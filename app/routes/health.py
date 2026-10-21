# app/routes/health.py
#
# Health check endpoint for Zuno.
# Used by deployment servers and monitoring tools
# to verify the API and database are running correctly.
#
# FIX: mysql is imported inside the function to avoid
# circular imports. app/__init__.py imports this file
# during initialization, so top-level imports from app
# would create a circular dependency.

from flask import Blueprint, jsonify
from datetime import datetime

health_bp = Blueprint('health', __name__)


@health_bp.route('/health', methods=['GET'])
def health_check():
    """
    GET /health

    Returns health status of the Zuno API.
    Checks both the API server and database connection.

    Returns 200 if everything is working.
    Returns 503 if database is down.
    """
    # Import mysql here instead of at top level
    # to avoid circular import with app/__init__.py
    from app import mysql

    health_status = {
        'status': 'healthy',
        'timestamp': str(datetime.now()),
        'service': 'Zuno API',
        'version': '1.0.0-mvp',
        'checks': {}
    }

    # Check database connection
    try:
        cursor = mysql.connection.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone()
        cursor.close()
        health_status['checks']['database'] = 'connected'
    except Exception as e:
        health_status['status'] = 'unhealthy'
        health_status['checks']['database'] = f'error: {str(e)}'

    status_code = (
        200 if health_status['status'] == 'healthy' else 503
    )

    return jsonify(health_status), status_code


@health_bp.route('/', methods=['GET'])
def root():
    """
    GET /
    Root endpoint — confirms API is running.
    Stops the 404 error when someone opens the base URL.
    """
    return jsonify({
        'message': 'Welcome to the Zuno API',
        'version': '1.0.0-mvp',
        'status': 'running',
        'documentation': 'Contact the Zuno team for API docs'
    }), 200