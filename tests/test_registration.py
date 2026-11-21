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
from models import AdminAuditEvent, RoleApplication, User


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
    assert response.json["redirect"] == "/dashboard"
    assert client.get("/dashboard").status_code == 200
    response = client.post(path, data=registration_data("student"))
    assert response.status_code == 409
    assert response.json["field"] == "email"


@pytest.mark.parametrize("role", ["mentor", "coach"])
def test_provider_registration_fields_cannot_grant_roles_or_create_applications(registration_app, monkeypatch, role):
    bucket = Mock()
    monkeypatch.setattr("routes.credentials_bucket", lambda: bucket)
    client = registration_app.test_client()
    data = registration_data(role)
    data.update(registration_intent=role, is_admin="true",
                bio="I have several years of relevant teaching and coaching experience.")
    data["proofs"] = (io.BytesIO(b"%PDF-1.4\n"), "proof.pdf")
    response = client.post("/api/auth/register", data=data)
    assert response.status_code == 201
    assert response.json["pending"] is False
    assert response.json["user"]["roles"] == ["student"]
    dashboard_url = f"/dashboard?onboarding={role}"
    assert response.json["redirect"] == dashboard_url
    heading = b"Teacher onboarding" if role == "mentor" else b"Coach onboarding"
    for _ in range(2):
        dashboard = client.get(dashboard_url)
        assert dashboard.status_code == 200
        assert heading in dashboard.data
        assert b"/settings#roles" in dashboard.data
    assert client.get("/schedule").status_code == 403
    assert client.post("/api/auth/login", json={
        "email": data["email"], "password": data["password"],
    }).status_code == 200
    assert client.get("/settings").status_code == 200
    bucket.upload.assert_not_called()
    with registration_app.app_context():
        assert RoleApplication.query.count() == 0


def test_unknown_registration_intent_does_not_change_account_access(registration_app):
    client = registration_app.test_client()
    data = registration_data("admin")
    data.update(registration_intent="admin", is_admin="true")
    response = client.post("/api/auth/register", data=data)
    assert response.status_code == 201
    assert response.json["redirect"] == "/dashboard"
    assert response.json["user"]["roles"] == ["student"]
    assert client.get("/admin/dashboard").status_code == 403


def test_admin_account_changes_require_boolean_and_record_actual_decisions(registration_app):
    with registration_app.app_context():
        admin = User(display_name="Admin", email="admin@example.test", password_hash="test", is_admin=True)
        student = User(display_name="Student", email="student@example.test", password_hash="test")
        db.session.add_all([admin, student])
        db.session.commit()
        admin_id, student_id = admin.id, student.id

    client = registration_app.test_client()
    set_admin = f"/api/admin/users/{student_id}/set-admin"
    toggle_active = f"/api/admin/users/{student_id}/toggle-active"
    with client.session_transaction() as session:
        session["_user_id"] = str(student_id)
    assert client.post(set_admin, json={"is_admin": True}).status_code == 403
    assert client.post(toggle_active, json={}).status_code == 403
    with client.session_transaction() as session:
        session["_user_id"] = str(admin_id)
    assert client.post(set_admin, json={"is_admin": "false"}).status_code == 400
    assert client.post(set_admin, json={}).status_code == 400
    assert client.post(set_admin, json={"is_admin": True}).json["is_admin"] is True
    assert client.post(set_admin, json={"is_admin": True}).json["is_admin"] is True
    assert client.post(toggle_active, json={}).json["is_active"] is False

    with registration_app.app_context():
        audit = AdminAuditEvent.query.order_by(AdminAuditEvent.id).all()
        assert [(row.action, row.before, row.after) for row in audit] == [
            ("admin_privilege_changed", {"is_admin": False}, {"is_admin": True}),
            ("account_active_changed", {"is_active": True}, {"is_active": False}),
        ]
        assert all(row.actor_id == admin_id and row.target_id == student_id for row in audit)


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
    assert b'id="providerNote"' in response.data
    assert b'name="roles"' not in response.data


def test_registration_notice_javascript():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the registration script test")
    subprocess.run([node, str(ROOT / "tests" / "registration_notice.cjs")], check=True)
