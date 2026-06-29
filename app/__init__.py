# This is the HEART of the Zuno Backend
# It creates the Flask Application and connects all the pieces together
# We use something called the "App Factory Pattern" - instead of creating the app directly at top level, we wrap it in a function called create_app(). This allows full control over configuration and makes testing much easier.
# Everything connect through here

from flask import Flask
from flask_mysqldb import MySQL
from flask_bcrypt import Bcrypt
from flask_jwt_extended import JWTManager
from dotenv import load_dotenv
from datetime import timedelta
import os

# Load all variables from our .env file into the environment
# This must happen before we read any os.getenv() calls
load_dotenv()

# ──EXTENSION INSTANCES ────────────────────────────────
# We create these here but do NOT attach them to the app yet.
# They get attached inside create_app() using .init_app(app).
# This is the correct pattern for larger Flask applications.

mysql = MySQL()       # handles all database connections
bcrypt = Bcrypt()     # handles password hashing
jwt = JWTManager()    # handles login token creation and verification

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

    # ── ATTACH EXTENSIONS TO APP ──────────────────────────────
    mysql.init_app(app)
    bcrypt.init_app(app)
    jwt.init_app(app)

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

     # ── START BACKGROUND JOBS ─────────────────────────────────
    # The scheduler runs automatic tasks in the background
    # while the server is running — like releasing funds after
    # the inspection window expires
    from app.services.scheduler import start_scheduler
    start_scheduler(app)

    return app


