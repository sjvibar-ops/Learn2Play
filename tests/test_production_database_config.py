import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
VALID_URL = "postgresql://audit-user:dummy-password@db.example.test:5432/learn2play"


def start_app(code="import wsgi", **settings):
    env = os.environ.copy()
    for name in ("APP_ENV", "VERCEL", "RENDER", "SECRET_KEY", "DATABASE_URL"):
        env.pop(name, None)
    env.update(settings)
    return subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("indicator", [
    {"APP_ENV": "production"},
    {"VERCEL": "1"},
    {"RENDER": "true"},
])
def test_production_requires_database_url(indicator):
    result = start_app(SECRET_KEY="x" * 32, **indicator)
    assert result.returncode != 0
    assert "Production requires DATABASE_URL to be set." in result.stderr


@pytest.mark.parametrize("url", [
    "sqlite:///local.db",
    "mysql://audit-user:dummy-password@db.example.test/app",
    "postgresql+asyncpg://audit-user:dummy-password@db.example.test/app",
    "postgresql://audit-user:dummy-password@db.example.test:invalid/app",
    "postgresql://audit-user:dummy-password@/app",
    "postgresql://audit-user:dummy-password@db.example.test/",
    VALID_URL + "?unknown_option=dummy-password",
    "invalid-dummy-password",
])
def test_production_rejects_invalid_database_url_without_exposing_it(url):
    result = start_app(APP_ENV="production", DATABASE_URL=url, SECRET_KEY="x" * 32)
    assert result.returncode != 0
    assert "Production requires DATABASE_URL to be a valid PostgreSQL URL" in result.stderr
    output = result.stdout + result.stderr
    assert url not in output
    assert "dummy-password" not in output


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "postgresql+psycopg2", "postgresql+psycopg"])
def test_production_uses_postgres_pool_without_connecting(scheme):
    result = start_app(
        """
from unittest.mock import patch
from psycopg import Connection

with patch.object(Connection, "connect", side_effect=AssertionError("Unexpected connection")):
    from extensions import db
    from wsgi import app

    with app.app_context():
        assert db.engine.url.drivername == "postgresql+psycopg"
        assert db.engine.url.database == "learn2play"
        assert db.engine.pool.size() == 3
    assert app.config["SQLALCHEMY_ENGINE_OPTIONS"] == {
        "pool_pre_ping": True,
        "pool_size": 3,
        "max_overflow": 2,
        "pool_recycle": 1800,
        "connect_args": {"sslmode": "require", "prepare_threshold": None},
    }
    response = app.test_client().get("/healthz")
    assert response.status_code == 200
    assert response.json == {"status": "ok"}
""",
        APP_ENV="production",
        DATABASE_URL=VALID_URL.replace("postgresql:", scheme + ":", 1),
        SECRET_KEY="x" * 32,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("app_env", ["development", "testing"])
def test_nonproduction_can_start_without_a_database_url(app_env):
    result = start_app(
        """
from pathlib import Path
from extensions import db
from wsgi import app

with app.app_context():
    assert db.engine.dialect.name == "sqlite"
    assert Path(db.engine.url.database) == Path.cwd() / "learn2play.db"
    assert app.config["SQLALCHEMY_ENGINE_OPTIONS"] == {"pool_pre_ping": True}
""",
        APP_ENV=app_env,
    )
    assert result.returncode == 0, result.stderr
