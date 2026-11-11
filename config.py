
import os
import secrets

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


class Config:
    # Production hosts must provide a stable, high-entropy secret explicitly.
    # Local development gets a process-local secret unless one is supplied.
    app_environment = os.environ.get("APP_ENV", "development").strip().lower()
    is_production = (
        app_environment == "production"
        or os.environ.get("VERCEL") == "1"
        or os.environ.get("RENDER") == "true"
    )
    secret_key = os.environ.get("SECRET_KEY", "").strip() or None
    if is_production and (not secret_key or len(secret_key) < 32):
        raise RuntimeError("Production requires a SECRET_KEY of at least 32 characters.")
    SECRET_KEY = secret_key or secrets.token_urlsafe(32)

    # Session and authentication cookie security
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SECURE = is_production
    SESSION_COOKIE_SAMESITE = "Lax"

    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SECURE = is_production
    REMEMBER_COOKIE_SAMESITE = "Lax"

    # JaaS credentials are server-only. Normalize escaped PEM newlines because
    # Vercel environment variables may provide the key as a single line.
    JAAS_APP_ID = os.environ.get("JAAS_APP_ID", "").strip()
    JAAS_API_KEY_ID = os.environ.get("JAAS_API_KEY_ID", "").strip()
    jaas_private_key = os.environ.get("JAAS_PRIVATE_KEY", "").strip()
    JAAS_PRIVATE_KEY = jaas_private_key.replace("\\n", "\n") if jaas_private_key else ""

    # Database
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        # Use the Psycopg 3 dialect installed by this project for PostgreSQL URLs.
        for old_scheme in ("postgres://", "postgresql://", "postgresql+psycopg2://"):
            if database_url.startswith(old_scheme):
                database_url = "postgresql+psycopg://" + database_url[len(old_scheme):]
                break

    SQLALCHEMY_DATABASE_URI = database_url or (
        "sqlite:///" + os.path.join(BASE_DIR, "learn2play.db")
    )
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}
    if database_url and database_url.startswith("postgresql+psycopg://"):
        # Conservative connection pool sized for Render Gunicorn workers
        # (2 workers x (3 pool + 2 overflow) = 10 max connections).
        # Preserves compatibility with both Supabase direct connections
        # (60 connection limit) and Supabase's shared transaction pooler.
        SQLALCHEMY_ENGINE_OPTIONS.update(
            pool_size=3,
            max_overflow=2,
            pool_recycle=1800,
            connect_args={"sslmode": "require", "prepare_threshold": None},
        )

    SQLALCHEMY_TRACK_MODIFICATIONS = False
