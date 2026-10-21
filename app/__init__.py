# This is the HEART of the Zuno Backend
# It creates the Flask Application and connects all the pieces together
# We use something called the "App Factory Pattern" - instead of creating the app directly at top level, we wrap it in a function called create_app(). This allows full control over configuration and makes testing much easier.
# Everything connect through here

from flask import Flask, jsonify
from flask_mysqldb import MySQL
from flask_bcrypt import Bcrypt
from flask_jwt_extended import JWTManager
from flask_mail import Mail                  # NEW — handles sending emails
from dotenv import load_dotenv
from datetime import timedelta
from app.routes.health import health_bp
import os

# Load all variables from our .env file into the environment
# This must happen before we read any os.getenv() calls
load_dotenv()

# ── EXTENSION INSTANCES ────────────────────────────────
# We create these here but do NOT attach them to the app yet.
# They get attached inside create_app() using .init_app(app).
# This is the correct pattern for larger Flask applications.

mysql = MySQL()       # handles all database connections
bcrypt = Bcrypt()     # handles password hashing
jwt = JWTManager()    # handles login token creation and verification
mail = Mail()         # NEW — handles sending emails to users

def create_app():
    """
    Creates, configures, and returns the Zuno Flask application.
    This function is called once when the server starts (in run.py).
    """
    app = Flask(__name__)

    # ── DATABASE CONFIGURATION ────────────────────────────────
    # These values come from our .env file via os.getenv()
    # The second argument is a default value if the .env key is missing
    app.config['MYSQL_HOST'] = os.getenv('DB_HOST', 'localhost')
    app.config['MYSQL_USER'] = os.getenv('DB_USER', 'root')
    app.config['MYSQL_PASSWORD'] = os.getenv('DB_PASSWORD', '')
    app.config['MYSQL_DB'] = os.getenv('DB_NAME', 'zuno_platform')

    # DictCursor makes MySQL return each row as a dictionary
    # So we can do row['phone_number'] instead of row[2]
    # Much more readable and safe
    app.config['MYSQL_CURSORCLASS'] = 'DictCursor'

    # ── SECURITY CONFIGURATION ────────────────────────────────
    app.config['SECRET_KEY'] = os.getenv('SECRET_KEY')
    app.config['JWT_SECRET_KEY'] = os.getenv('JWT_SECRET_KEY')

    # Tokens expire after 24 hours — user must log in again after that
    app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(
        hours=int(os.getenv('JWT_ACCESS_TOKEN_EXPIRES_HOURS', 24))
    )

    # ── JWT CONFIGURATION ─────────────────────────────────────
    # Tell JWT-Extended that our identity is a simple string
    # This is required for version 4.x compatibility
    app.config['JWT_IDENTITY_CLAIM'] = 'sub'

    # ── EMAIL CONFIGURATION ───────────────────────────────────
    # These settings tell Flask-Mail how to connect to Gmail
    # and send emails on behalf of Zuno.
    # All values come from .env — never hardcode credentials here.
    app.config['MAIL_SERVER'] = os.getenv('MAIL_SERVER', 'smtp.gmail.com')
    app.config['MAIL_PORT'] = int(os.getenv('MAIL_PORT', 587))
    app.config['MAIL_USE_TLS'] = os.getenv('MAIL_USE_TLS', 'True') == 'True'
    app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')
    app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')
    app.config['MAIL_DEFAULT_SENDER'] = os.getenv('MAIL_DEFAULT_SENDER')

    # ── ATTACH EXTENSIONS TO APP ──────────────────────────────
    mysql.init_app(app)
    bcrypt.init_app(app)
    jwt.init_app(app)
    mail.init_app(app)      # NEW — attach mail to app

    # ── REGISTER ROUTE BLUEPRINTS ─────────────────────────────
    # A Blueprint is a group of related routes.
    # We keep routes in separate files to stay organized.
    # url_prefix means every route in that file starts with that path.
    #
    # Example: auth_bp has a route '/login'
    #          With prefix '/api/auth', the full path is '/api/auth/login'

    from app.routes.auth import auth_bp
    from app.routes.contracts import contracts_bp
    from app.routes.transactions import transactions_bp
    from app.routes.payments import payments_bp
    from app.routes.courier import courier_bp
    from app.routes.disputes import disputes_bp
    from app.routes.negotiation import negotiation_bp
    from app.routes.reputation import reputation_bp
    from app.routes.catalog import catalog_bp
    from app.routes.admin import admin_bp

    app.register_blueprint(auth_bp,         url_prefix='/api/auth')
    app.register_blueprint(contracts_bp,    url_prefix='/api/contracts')
    app.register_blueprint(transactions_bp, url_prefix='/api/transactions')
    app.register_blueprint(payments_bp,     url_prefix='/api/payments')
    app.register_blueprint(courier_bp,      url_prefix='/api/courier')
    app.register_blueprint(disputes_bp,     url_prefix='/api/disputes')
    app.register_blueprint(negotiation_bp,  url_prefix='/api/negotiation')
    app.register_blueprint(reputation_bp,   url_prefix='/api/reputation')
    app.register_blueprint(catalog_bp,      url_prefix='/api/catalog')
    app.register_blueprint(admin_bp,        url_prefix='/api/admin')
    app.register_blueprint(health_bp, url_prefix='')

    # ── START BACKGROUND JOBS ─────────────────────────────────
    # The scheduler runs automatic tasks in the background
    # while the server is running — like releasing funds after
    # the inspection window expires
    from app.services.scheduler import start_scheduler
    start_scheduler(app)


    # ── CORS CONFIGURATION ───────────────────────────────────
    # CORS allows your frontend (running on a different port
    # or domain) to make requests to this API.
    # Without this, browsers block cross-origin requests.
    from flask_cors import CORS
    CORS(app, resources={
        r"/api/*": {
            # In production replace * with your actual domain
            # e.g. "https://zuno.co.ke"
            "origins": "*",
            "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            "allow_headers": ["Content-Type", "Authorization"]
        }
    })

        # ── SECURITY HEADERS ──────────────────────────────────────
    # Add security headers to every response automatically
    from app.utils.security import add_security_headers
    app.after_request(add_security_headers)

    # ── GLOBAL ERROR HANDLERS ─────────────────────────────────
    # Register clean JSON error responses for all HTTP errors
    from app.utils.errors import register_error_handlers
    register_error_handlers(app)

   

    return app

# ── JWT ERROR HANDLERS ────────────────────────────────────────
# These are defined outside create_app() so VS Code can properly
# resolve the parameters and imports.
# jwt is the JWTManager instance created at the top of this file.
# Flask-JWT-Extended calls these automatically when token errors occur.

@jwt.unauthorized_loader
def missing_token_callback(error):
    """Called when a protected route is accessed with no token."""
    return jsonify({
        'error': 'Authorization required',
        'message': 'No token provided. Please log in.',
        'status': 401
    }), 401


@jwt.invalid_token_loader
def invalid_token_callback(error):
    """Called when a token is malformed or tampered with."""
    return jsonify({
        'error': 'Invalid token',
        'message': 'Your token is invalid. Please log in again.',
        'status': 401
    }), 401


@jwt.expired_token_loader
def expired_token_callback(jwt_header, jwt_data):
    """
    Called when a valid token has expired.
    jwt_header: the decoded header of the expired token
    jwt_data:   the decoded payload of the expired token
    Both are passed automatically by Flask-JWT-Extended.
    """
    return jsonify({
        'error': 'Token expired',
        'message': 'Your session expired. Please log in again.',
        'status': 401
    }), 401