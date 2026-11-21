from flask import Flask, request
from werkzeug.exceptions import RequestEntityTooLarge

if __package__:
    from .config import Config
    from .errors import register_error_handlers
    from .extensions import csrf, db, login_manager, migrate
else:
    from config import Config
    from errors import register_error_handlers
    from extensions import csrf, db, login_manager, migrate


def create_app(config_overrides=None):
    app = Flask(__name__)

    # Load application settings
    app.config.from_object(Config)
    if config_overrides is not None:
        app.config.update(config_overrides)

    @app.before_request
    def enforce_request_limits():
        # Register before CSRF: CSRF itself may parse request.form.
        if request.endpoint == "payments.submit":
            request.max_content_length = min(
                app.config["PAYMENT_REQUEST_MAX_BYTES"],
                app.config["MAX_CONTENT_LENGTH"],
            )
        limit = request.max_content_length
        if limit is not None and request.content_length is not None and request.content_length > limit:
            raise RequestEntityTooLarge()

    # Initialize extensions
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)

    # Import models so SQLAlchemy knows about them
    if __package__:
        from .models import Appointment, Credential, MentorAvailability, MentorUnavailableDate, Message, Report, RoleApplication, User
    else:
        from models import Appointment, Credential, MentorAvailability, MentorUnavailableDate, Message, Report, RoleApplication, User

    # Tell Flask-Login how to find a user by ID
    @login_manager.user_loader
    def load_user(user_id):
        try:
            user = db.session.get(User, int(user_id))
            # Flask-Login reloads this on each request. Returning None immediately
            # invalidates sessions for accounts deactivated since the last login.
            return user if user and user.is_active else None
        except (ValueError, TypeError):
            return None

    # Register application routes
    if __package__:
        from .routes import main
    else:
        from routes import main
    app.register_blueprint(main)
    if __package__:
        from .payments import payments
    else:
        from payments import payments
    app.register_blueprint(payments)

    register_error_handlers(app)

    # Support reverse proxies (Vercel edge -> Render) by trusting the immediate
    # proxy hop's forwarded headers for client IP, protocol, host, and port.
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=1,
        x_proto=1,
        x_host=1,
        x_port=1,
        x_prefix=0,
    )

    return app
