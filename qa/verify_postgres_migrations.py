"""Verify Alembic migrations against disposable PostgreSQL (Feature 3 CI/CD Finalization).

SAFETY INVARIANTS:
- NEVER connects to production, Supabase, Render, or persistent databases.
- Requires BOTH an explicit authorization sentinel and a validated loopback URL.
- Database name MUST start with 'l2p_disposable_' or 'learn2play_disposable_'.
- Verifies exact linear head 'd0a1b2c3d4e5' and all 10 revision steps.
"""

from datetime import datetime, timezone
import os
import sys

import sqlalchemy as sa
from sqlalchemy.engine import make_url

SENTINEL = "YES_I_AUTHORIZE_DISPOSABLE_TEST_DB"
ALLOWED_DB_PREFIXES = ("l2p_disposable_", "learn2play_disposable_")
EXPECTED_HEAD = "d0a1b2c3d4e5"
EXPECTED_REVISION_COUNT = 10


def get_verified_disposable_url():
    if os.environ.get("L2P_DISPOSABLE_PG_TESTS") != SENTINEL:
        raise RuntimeError("Missing or invalid L2P_DISPOSABLE_PG_TESTS authorization sentinel.")
    raw = os.environ.get("L2P_DISPOSABLE_PG_URL", "")
    if not raw:
        raise RuntimeError("L2P_DISPOSABLE_PG_URL must be set.")
    u = make_url(raw)

    if u.drivername not in ("postgresql", "postgresql+psycopg", "postgresql+psycopg2"):
        raise RuntimeError(f"Unsupported driver: {u.drivername}")
    if u.host not in ("127.0.0.1", "localhost"):
        raise RuntimeError(f"Host must be loopback only, got: {u.host}")
    if not any((u.database or "").startswith(p) for p in ALLOWED_DB_PREFIXES):
        raise RuntimeError(f"Database name must start with one of {ALLOWED_DB_PREFIXES}, got: {u.database}")

    return raw


def verify_migrations():
    url = get_verified_disposable_url()
    print(f"[{datetime.now(timezone.utc).isoformat()}] Verified disposable PostgreSQL URL: host={make_url(url).host}, db={make_url(url).database}")

    # Set up environment for Flask app factory
    os.environ["DATABASE_URL"] = url
    os.environ["FLASK_ENV"] = "testing"
    os.environ["SECRET_KEY"] = "disposable-test-secret-key-32chars-minimum-entropy"

    # Determine adaptive connect_args (support both SSL-enabled and fresh CI containers)
    connect_args = {"prepare_threshold": None}
    try:
        test_engine = sa.create_engine(
            url,
            connect_args={"sslmode": "require", "connect_timeout": 2, "prepare_threshold": None},
        )
        with test_engine.connect() as test_conn:
            test_conn.execute(sa.text("SELECT 1"))
        connect_args["sslmode"] = "require"
        test_engine.dispose()
    except Exception:
        connect_args["sslmode"] = "prefer"

    from Backend import create_app, db
    from flask_migrate import upgrade
    from alembic.migration import MigrationContext
    from alembic.script import ScriptDirectory

    app = create_app()
    with app.app_context():
        engine = sa.create_engine(
            app.config["SQLALCHEMY_DATABASE_URI"],
            connect_args=connect_args,
        )
        db.engines[None] = engine

        print("Applying Alembic migrations to disposable PostgreSQL...")
        upgrade(directory="Backend/migrations")
        print("Alembic upgrade completed successfully.")

        # Verify revision in alembic_version table
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            current_rev = ctx.get_current_revision()
            print(f"Current database revision: {current_rev}")
            if current_rev != EXPECTED_HEAD:
                raise AssertionError(f"Expected DB revision {EXPECTED_HEAD}, got {current_rev}")

            script = ScriptDirectory("Backend/migrations")
            heads = script.get_heads()
            print(f"Alembic script heads: {heads}")
            if heads != [EXPECTED_HEAD]:
                raise AssertionError(f"Expected single linear head [{EXPECTED_HEAD}], got {heads}")

            # Verify history chain count
            revisions = list(script.walk_revisions())
            print(f"Total revisions in migration history: {len(revisions)}")
            if len(revisions) != EXPECTED_REVISION_COUNT:
                raise AssertionError(f"Expected {EXPECTED_REVISION_COUNT} revisions, got {len(revisions)}")

            # Verify schema
            insp = sa.inspect(conn)
            tables = insp.get_table_names()
            required_tables = ["appointments", "discord_channel_recoveries", "users"]
            for t in required_tables:
                if t not in tables:
                    raise AssertionError(f"Required table {t} missing from database!")

            cols = [c["name"] for c in insp.get_columns("discord_channel_recoveries")]
            required_cols = ["cleanup_fencing_token", "deletion_dispatched_at", "deletion_http_status", "cleanup_state"]
            for c in required_cols:
                if c not in cols:
                    raise AssertionError(f"Required column {c} missing from discord_channel_recoveries!")

        engine.dispose()
    print("ALL MIGRATION VERIFICATIONS PASSED (10/10 revisions, linear head d0a1b2c3d4e5, schema consistent)!")


if __name__ == "__main__":
    verify_migrations()
