import copy
from collections.abc import Mapping
from pathlib import Path

from flask import Flask
from sqlalchemy.engine import make_url

if __package__:
    from .config import Config, build_engine_options, normalize_database_url
    from .errors import register_error_handlers
    from .extensions import csrf, db, login_manager, migrate
else:
    from config import Config, build_engine_options, normalize_database_url
    from errors import register_error_handlers
    from extensions import csrf, db, login_manager, migrate

BACKEND_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BACKEND_DIR.parent / "Frontend"


# ---------------------------------------------------------------------------
# Guard for an explicitly supplied test configuration.
#
# These rules apply ONLY when create_app() receives a test_config. The normal
# create_app() path is not restricted: it still loads Config exactly as before.
# ---------------------------------------------------------------------------
class UnsafeTestConfigError(ValueError):
    """test_config could have pointed the application at a real database.

    Messages are fixed strings and never include the rejected value, so a
    connection string cannot leak through an error message.
    """


# SQLAlchemy settings a caller may supply through test_config. Every other
# setting that starts with SQLALCHEMY_ is refused, which includes
# SQLALCHEMY_ENGINE_OPTIONS (the factory builds safe options itself) and any
# secondary-database setting.
_TEST_SQLALCHEMY_KEYS = frozenset({
    "SQLALCHEMY_DATABASE_URI",
    "SQLALCHEMY_TRACK_MODIFICATIONS",
    "SQLALCHEMY_ECHO",
})
# The merged configuration additionally holds SQLALCHEMY_ENGINE_OPTIONS because
# the factory sets it itself after validation.
_MERGED_SQLALCHEMY_KEYS = _TEST_SQLALCHEMY_KEYS | {"SQLALCHEMY_ENGINE_OPTIONS"}

# Exact URI strings verified to open a private in-memory SQLite database.
# Other spellings ("sqlite://", "sqlite:///", "sqlite+pysqlite:///:memory:")
# are PENDING VERIFICATION and stay refused until the real-engine probe in
# tests/test_app_factory.py has been run and reviewed on the real repository.
_VERIFIED_MEMORY_SQLITE_URIS = frozenset({"sqlite:///:memory:"})
_MEMORY_SQLITE_DRIVERS = frozenset({"sqlite", "sqlite+pysqlite"})


def _is_in_memory_sqlite_url(url):
    """Structural check on a parsed SQLAlchemy URL: in-memory SQLite, no extras."""
    return (
        url.drivername in _MEMORY_SQLITE_DRIVERS
        and url.database in (None, "", ":memory:")
        and not url.query
        and url.host is None
        and url.port is None
        and url.username is None
        and url.password is None
    )


def _validate_test_database_uri(uri):
    """Accept only a verified in-memory SQLite URI string."""
    if not isinstance(uri, str) or not uri.strip():
        raise UnsafeTestConfigError("SQLALCHEMY_DATABASE_URI must be a non-empty string.")
    if uri != uri.strip():
        raise UnsafeTestConfigError(
            "SQLALCHEMY_DATABASE_URI must not have leading or trailing whitespace."
        )
    if uri not in _VERIFIED_MEMORY_SQLITE_URIS:
        raise UnsafeTestConfigError("test_config only permits a verified in-memory SQLite URI.")
    try:
        parsed = make_url(uri)
    except Exception:
        raise UnsafeTestConfigError("SQLALCHEMY_DATABASE_URI could not be parsed.") from None
    if not _is_in_memory_sqlite_url(parsed):
        raise UnsafeTestConfigError("test_config only permits a verified in-memory SQLite URI.")


def _prepare_test_config(test_config):
    """Return a validated deep copy of test_config; the caller's object is untouched."""
    if not isinstance(test_config, Mapping):
        raise UnsafeTestConfigError("test_config must be a mapping.")
    prepared = copy.deepcopy(dict(test_config))

    for key in prepared:
        if not isinstance(key, str):
            raise UnsafeTestConfigError("test_config settings must use string keys.")
        upper_key = key.upper()
        if upper_key == "SQLALCHEMY_ENGINE_OPTIONS":
            raise UnsafeTestConfigError(
                "test_config must not set SQLALCHEMY_ENGINE_OPTIONS; "
                "the factory supplies safe engine options."
            )
        if upper_key in ("SQLALCHEMY_BINDS", "SQLALCHEMY_ENGINES"):
            raise UnsafeTestConfigError(
                "test_config must not configure secondary SQLAlchemy databases."
            )
        if upper_key.startswith("SQLALCHEMY_") and key not in _TEST_SQLALCHEMY_KEYS:
            raise UnsafeTestConfigError("test_config contains an unsupported SQLALCHEMY_* setting.")

    if "SQLALCHEMY_DATABASE_URI" not in prepared:
        raise UnsafeTestConfigError("test_config must provide SQLALCHEMY_DATABASE_URI.")
    uri = prepared["SQLALCHEMY_DATABASE_URI"]
    if not isinstance(uri, str) or not uri.strip():
        raise UnsafeTestConfigError("SQLALCHEMY_DATABASE_URI must be a non-empty string.")
    prepared["SQLALCHEMY_DATABASE_URI"] = normalize_database_url(uri)
    return prepared


def _reject_unsafe_merged_sqlalchemy_config(config):
    """Refuse SQLAlchemy settings that were inherited from Config or defaults."""
    for key in config:
        if (
            isinstance(key, str)
            and key.upper().startswith("SQLALCHEMY_")
            and key not in _MERGED_SQLALCHEMY_KEYS
        ):
            raise UnsafeTestConfigError(
                "The merged application configuration contains SQLAlchemy settings "
                "that are not permitted with test_config."
            )


def create_app(test_config=None):
    # With an explicit test_config, validate everything BEFORE a Flask app or
    # any SQLAlchemy engine is created.
    prepared_test_config = None if test_config is None else _prepare_test_config(test_config)

    app = Flask(
        __name__,
        template_folder=str(FRONTEND_DIR / "templates"),
        static_folder=str(FRONTEND_DIR / "static"),
        static_url_path="/static",
    )

    # Load application settings
    app.config.from_object(Config)

    if prepared_test_config is None:
        # Normal runtime path: same values as before, but copied so that separate
        # app instances never share the class-level engine-options dictionaries.
        if "SQLALCHEMY_ENGINE_OPTIONS" in app.config:
            app.config["SQLALCHEMY_ENGINE_OPTIONS"] = copy.deepcopy(
                app.config["SQLALCHEMY_ENGINE_OPTIONS"]
            )
    else:
        app.config.update(prepared_test_config)
        # Validate the URI that will actually be handed to SQLAlchemy.
        _validate_test_database_uri(app.config.get("SQLALCHEMY_DATABASE_URI"))
        _reject_unsafe_merged_sqlalchemy_config(app.config)
        # Replace anything inherited from Config with fresh options for this URI.
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = build_engine_options(
            app.config["SQLALCHEMY_DATABASE_URI"]
        )

    # Initialize extensions
    db.init_app(app)
    migrate.init_app(app, db, directory=str(BACKEND_DIR / "migrations"))
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

    register_error_handlers(app)

    return app
