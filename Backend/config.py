
import os
import secrets

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

_POSTGRES_PSYCOPG_PREFIX = "postgresql+psycopg://"
_LEGACY_POSTGRES_PREFIXES = ("postgres://", "postgresql://", "postgresql+psycopg2://")


def normalize_database_url(url):
    """Return the SQLAlchemy URL this project uses for a configured database URL.

    Legacy PostgreSQL schemes are rewritten to the Psycopg 3 dialect installed by
    this project. Matching is deliberately exact: it is case-sensitive, it does
    not strip whitespace, and other schemes (for example postgresql+asyncpg://)
    are returned untouched. Falsy values are returned unchanged.
    """
    if url:
        for old_scheme in _LEGACY_POSTGRES_PREFIXES:
            if url.startswith(old_scheme):
                return _POSTGRES_PSYCOPG_PREFIX + url[len(old_scheme):]
    return url


def build_engine_options(uri):
    """Return a NEW SQLAlchemy engine-options dict for ``uri``.

    A fresh dictionary, with fresh nested dictionaries, is returned on every
    call so that separate apps and tests never share or mutate each other's
    options.
    """
    options = {"pool_pre_ping": True}
    if isinstance(uri, str) and uri.startswith(_POSTGRES_PSYCOPG_PREFIX):
        # Bound each warm serverless instance to one connection and configure
        # Psycopg for Supabase's shared transaction pooler.
        options.update(
            pool_size=1,
            max_overflow=0,
            connect_args={"sslmode": "require", "prepare_threshold": None},
        )
    return options


class Config:
    # Production hosts must provide a stable, high-entropy secret explicitly.
    # Local development gets a process-local secret unless one is supplied.
    app_environment = os.environ.get("APP_ENV", "development").strip().lower()
    is_production = app_environment == "production" or os.environ.get("VERCEL") == "1"
    secret_key = os.environ.get("SECRET_KEY", "").strip() or None
    if is_production and (not secret_key or len(secret_key) < 32):
        raise RuntimeError("Production requires a SECRET_KEY of at least 32 characters.")
    SECRET_KEY = secret_key or secrets.token_urlsafe(32)

    # JaaS credentials are server-only. Normalize escaped PEM newlines because
    # Vercel environment variables may provide the key as a single line.
    JAAS_APP_ID = os.environ.get("JAAS_APP_ID", "").strip()
    JAAS_API_KEY_ID = os.environ.get("JAAS_API_KEY_ID", "").strip()
    jaas_private_key = os.environ.get("JAAS_PRIVATE_KEY", "").strip()
    JAAS_PRIVATE_KEY = jaas_private_key.replace("\\n", "\n") if jaas_private_key else ""

    # Discord OAuth2 (Batch 3.1A). All values are optional: LEARN2PLAY starts,
    # registers, logs in and runs JaaS meetings without them. They are server-only
    # secrets/IDs, validated only when the Discord authorization flow starts, and
    # must never be rendered into templates, API responses or logs.
    # Local redirect URI (must match the Discord Developer Portal exactly):
    #   http://127.0.0.1:5000/api/auth/discord/callback
    DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "").strip()
    DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "").strip()
    DISCORD_REDIRECT_URI = os.environ.get("DISCORD_REDIRECT_URI", "").strip()
    DISCORD_GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "").strip()
    DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    # Explicit opt-in: reconciliation and live readiness must be approved first.
    DISCORD_SESSIONS_ENABLED = os.environ.get("DISCORD_SESSIONS_ENABLED", "").lower() == "true"

    # Database
    # Use the Psycopg 3 dialect installed by this project for PostgreSQL URLs.
    database_url = normalize_database_url(os.environ.get("DATABASE_URL"))

    SQLALCHEMY_DATABASE_URI = database_url or (
        "sqlite:///" + os.path.join(BASE_DIR, "learn2play.db")
    )
    # Engine options follow the final URI and are built fresh for this class.
    SQLALCHEMY_ENGINE_OPTIONS = build_engine_options(SQLALCHEMY_DATABASE_URI)

    SQLALCHEMY_TRACK_MODIFICATIONS = False
