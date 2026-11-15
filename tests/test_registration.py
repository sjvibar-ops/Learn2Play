import importlib
import io
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import Mock

import pytest
from sqlalchemy import inspect

from __init__ import create_app
from config import Config
from extensions import db
from models import RoleApplication


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def registration_app(tmp_path, monkeypatch):
    database = tmp_path / "local.db"
    monkeypatch.setattr(Config, "SQLALCHEMY_DATABASE_URI", f"sqlite:///{database}")
    monkeypatch.setattr(Config, "SQLALCHEMY_ENGINE_OPTIONS", {"pool_pre_ping": True})
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app.app_context():
        db.create_all()
        assert db.engine.dialect.name == "sqlite"
        assert Path(db.engine.url.database) == database
        assert set(db.metadata.tables) <= set(inspect(db.engine).get_table_names())
    return app


def test_default_local_database_selection(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    subprocess.run(
        [sys.executable, "-c", """
from pathlib import Path
from app import app
from extensions import db
with app.app_context():
    assert db.engine.dialect.name == "sqlite"
    assert Path(db.engine.url.database) == Path.cwd() / "learn2play.db"
"""],
        cwd=ROOT,
        check=True,
    )


def registration_data(role):
    return {
        "roles": role,
        "name": "Local Test User",
        "email": "local@example.test",
        "password": "StrongPassword1",
        "confirm_password": "StrongPassword1",
        "acknowledged": "true",
    }


def test_student_registration_and_duplicate_lookup(registration_app):
    client = registration_app.test_client()
    path = "/api/auth/register"
    response = client.post(path, data=registration_data("student"))
    assert response.status_code == 201
    assert response.json["pending"] is False
    response = client.post(path, data=registration_data("student"))
    assert response.status_code == 409
    assert response.json["field"] == "email"


@pytest.mark.parametrize("role", ["mentor", "coach"])
def test_verified_registration_requires_proof_and_creates_pending_application(
    registration_app, monkeypatch, role
):
    bucket = Mock()
    monkeypatch.setattr("routes.credentials_bucket", lambda: bucket)
    client = registration_app.test_client()
    data = registration_data(role)
    response = client.post("/api/auth/register", data=data)
    assert response.status_code == 400
    assert response.json["field"] == "proofs"
    bucket.upload.assert_not_called()

    data.update(bio="I have several years of relevant teaching and coaching experience.")
    data["proofs"] = (io.BytesIO(b"%PDF-1.4\n"), "proof.pdf")
    response = client.post("/api/auth/register", data=data)
    assert response.status_code == 201
    assert response.json["pending"] is True
    bucket.upload.assert_called_once()
    with registration_app.app_context():
        application = RoleApplication.query.one()
        assert (application.role, application.status) == (role, "pending")


def test_package_error_handler_rolls_back_and_preserves_responses(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT.parent))
    package = importlib.import_module(ROOT.name)
    monkeypatch.setattr(package.Config, "SQLALCHEMY_DATABASE_URI", "sqlite://")
    monkeypatch.setattr(package.Config, "SQLALCHEMY_ENGINE_OPTIONS", {})
    app = package.create_app()
    app.config["PROPAGATE_EXCEPTIONS"] = False
    rollback = Mock()
    monkeypatch.setattr(package.db.session, "rollback", rollback)

    @app.get("/fail")
    @app.get("/api/fail")
    def fail():
        raise RuntimeError("test failure")

    client = app.test_client()
    response = client.get("/api/fail")
    assert response.status_code == 500
    assert response.json == {
        "ok": False,
        "message": "Something went wrong. Please try again later.",
    }
    response = client.get("/fail")
    assert response.status_code == 500
    assert b"Something went wrong. Please try again later." in response.data
    assert rollback.call_count == 2


def test_registration_notice_follows_role_changes(registration_app):
    response = registration_app.test_client().get("/register")
    assert response.status_code == 200
    assert b'class="warning hidden" id="verificationNotice"' in response.data


def test_registration_notice_javascript():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the registration script test")
    subprocess.run([node, str(ROOT / "tests" / "registration_notice.cjs")], check=True)
