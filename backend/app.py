import sys
import os

# Get the absolute path to the project root
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(BASE_DIR, "src")
REDIRX_DIR = os.path.join(SRC_DIR, "redirx")
BACKEND_DIR = os.path.dirname(__file__)

# Add directories to Python path
sys.path.insert(0, BASE_DIR)  # Add project root so 'backend' module is found
sys.path.insert(0, SRC_DIR)
sys.path.insert(0, REDIRX_DIR)

from flask import Flask
from flask_cors import CORS
from backend.routes.pipeline_routes import pipeline_blueprint
from backend.routes.auth_routes import auth_blueprint
from backend.routes.user_routes import user_blueprint
from backend.routes.demo_routes import demo_blueprint
from backend.routes.url_match_routes import url_match_blueprint
from backend.routes.billing_routes import billing_blueprint, pricing_blueprint
from backend.routes.email_routes import email_blueprint
from backend.routes.gsc_routes import gsc_blueprint
from backend.routes.discovery_routes import discovery_blueprint
from backend.routes.api_key_routes import api_key_blueprint
from backend.routes.watch_routes import watch_blueprint
from backend.routes.v1_routes import v1_blueprint
from backend.routes.internal_routes import internal_blueprint
from backend.extensions import limiter, register_error_handlers

def create_app():
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = int(
        os.getenv("MAX_CONTENT_LENGTH", str(25 * 1024 * 1024))
    )

    # Configure CORS - restrict origins in production
    # Set CORS_ORIGINS env var to comma-separated list of allowed origins
    # e.g., "https://redirx.onrender.com,https://www.redirx.com"
    cors_origins = os.getenv('CORS_ORIGINS', '*')
    if cors_origins != '*':
        cors_origins = [origin.strip() for origin in cors_origins.split(',')]
    CORS(app, origins=cors_origins)
    limiter.init_app(app)
    register_error_handlers(app)

    app.register_blueprint(pipeline_blueprint, url_prefix="/api")
    app.register_blueprint(auth_blueprint, url_prefix="/api/auth")
    app.register_blueprint(user_blueprint, url_prefix="/api/user")
    app.register_blueprint(demo_blueprint, url_prefix="/api/demo")
    app.register_blueprint(url_match_blueprint, url_prefix="/api")
    app.register_blueprint(pricing_blueprint, url_prefix="/api/pricing")
    app.register_blueprint(billing_blueprint, url_prefix="/api/billing")
    app.register_blueprint(email_blueprint, url_prefix="/api/email")
    app.register_blueprint(gsc_blueprint, url_prefix="/api/gsc")
    app.register_blueprint(discovery_blueprint, url_prefix="/api/discovery")
    app.register_blueprint(api_key_blueprint, url_prefix="/api/keys")
    app.register_blueprint(watch_blueprint, url_prefix="/api/watches")
    # Public, agent-facing. Versioned because agents pin to it.
    app.register_blueprint(v1_blueprint, url_prefix="/api/v1")
    # Dormant pivot API: activation is an explicit release setting.
    if os.getenv("MCP_PIVOT_ENABLED", "false").lower() == "true":
        from backend.routes.v2_routes import v2_blueprint
        from backend.routes.migration_checkout_routes import create_migration_checkout_blueprint
        from backend.routes.current_billing_routes import create_current_billing_blueprint
        from backend.routes.migration_gsc_routes import create_migration_gsc_blueprint
        from backend.routes.migration_discovery_routes import create_migration_discovery_blueprint
        from backend.routes.migration_mapping_routes import create_migration_mapping_blueprint
        from backend.routes.migration_outcome_routes import create_migration_outcome_blueprint
        from backend.routes.migration_artifact_routes import create_migration_artifact_blueprint
        app.register_blueprint(v2_blueprint, url_prefix="/api/v2")
        app.register_blueprint(create_migration_checkout_blueprint(), url_prefix="/api/v2")
        app.register_blueprint(create_current_billing_blueprint(), url_prefix="/api/v2")
        app.register_blueprint(create_migration_gsc_blueprint(), url_prefix="/api/v2")
        app.register_blueprint(create_migration_discovery_blueprint(), url_prefix="/api/v2")
        app.register_blueprint(create_migration_mapping_blueprint(), url_prefix="/api/v2")
        app.register_blueprint(create_migration_outcome_blueprint(), url_prefix="/api/v2")
        app.register_blueprint(create_migration_artifact_blueprint(), url_prefix="/api/v2")
    # Service-to-service only (shared-secret protected) — called by the
    # mcp-server gateway, never by a browser or an agent's own API key.
    app.register_blueprint(internal_blueprint, url_prefix="/api/internal")

    @app.route("/")
    def home():
        return "Redirx backend is running!"

    # Debug routes - only available in development
    if os.getenv('FLASK_ENV') == 'development' or os.getenv('FLASK_DEBUG') == '1':
        @app.route("/api/debug/routes")
        def debug_routes():
            """Debug endpoint to list all registered routes"""
            routes = []
            for rule in app.url_map.iter_rules():
                routes.append({
                    'endpoint': rule.endpoint,
                    'methods': list(rule.methods),
                    'path': str(rule)
                })
            return {'routes': sorted(routes, key=lambda x: x['path'])}

    return app

if __name__ == "__main__":
    app = create_app()
    app.run(debug=True, port=5001)
