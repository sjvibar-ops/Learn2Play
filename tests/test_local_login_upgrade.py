import pytest
from flask_migrate import upgrade
from sqlalchemy import text
from werkzeug.security import generate_password_hash

from __init__ import create_app
from extensions import db
from models import RoleApplication, User


PASSWORD = "LocalFixture1"


def test_upgrading_old_sqlite_restores_login_and_preserves_account(tmp_path):
    app = create_app({
        "TESTING": True, "WTF_CSRF_ENABLED": False,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'upgrade.db'}",
    })
    with app.app_context():
        upgrade(revision="9e45f7e7aa32")
        db.session.execute(text("""
            INSERT INTO users (id, display_name, email, password_hash, roles_json, is_admin, is_active)
            VALUES (42, 'Existing Learner', 'existing@example.test', :password_hash, '["student"]', 0, 1)
        """), {"password_hash": generate_password_hash(PASSWORD)})
        db.session.commit()

    client = app.test_client()
    credentials = {"email": "existing@example.test", "password": PASSWORD}
    stale = client.post("/api/auth/login", json=credentials)
    assert stale.status_code == 503
    assert "schema upgrade" in stale.json["message"]
    assert "leaderboard_opt_in" not in stale.get_data(as_text=True)
    with client.session_transaction() as session:
        assert "_user_id" not in session

    with app.app_context():
        upgrade()
        assert db.session.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "db71a6e5c902"
        indexes = {row[1]: (row[2], row[4]) for row in db.session.execute(text("PRAGMA index_list(payments)"))}
        assert indexes["uq_payments_verified_transfer"] == (1, 1)
        assert indexes["ix_payments_transfer_claim"] == (0, 0)
        assert db.session.execute(text("PRAGMA foreign_key_check")).all() == []
        user = db.session.get(User, 42)
        assert user.check_password(PASSWORD)
        assert user.leaderboard_opt_in is False
        assert user.roles == ["student"]

    assert client.post("/api/auth/login", json=credentials).status_code == 200
    assert client.get("/dashboard").status_code == 200


@pytest.fixture
def login_app():
    app = create_app({
        "TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite://",
    })
    with app.app_context():
        db.create_all()
    return app


@pytest.mark.parametrize(("roles", "is_admin", "pending"), [
    (["student"], False, None),
    (["student"], True, None),
    (["student", "mentor"], False, None),
    (["student", "coach"], False, None),
    (["student"], False, "mentor"),
    ([], False, "coach"),
])
def test_login_preserves_permissions(login_app, roles, is_admin, pending):
    with login_app.app_context():
        user = User(display_name="Login Fixture", email="login@example.test", is_admin=is_admin)
        user.roles = roles
        user.set_password(PASSWORD)
        db.session.add(user)
        db.session.flush()
        if pending:
            db.session.add(RoleApplication(user_id=user.id, role=pending, bio="Qualification evidence", status="pending"))
        db.session.commit()
    client = login_app.test_client()
    response = client.post("/api/auth/login", json={
        "email": " LOGIN@example.test ", "password": PASSWORD,
        "roles": ["mentor", "coach"], "is_admin": True,
    })
    assert response.status_code == 200
    assert set(response.json["user"]["roles"]) == set(roles)
    assert response.json["redirect"] == ("/admin/dashboard" if is_admin else "/dashboard")
    assert client.get("/schedule").status_code == (200 if {"mentor", "coach"}.intersection(roles) else 403)


def test_login_rejects_bad_credentials_inactive_accounts_and_non_objects(login_app):
    with login_app.app_context():
        user = User(display_name="Inactive", email="inactive@example.test", is_active=False)
        user.set_password(PASSWORD)
        db.session.add(user)
        db.session.commit()

    client = login_app.test_client()
    for email, password, status in (
        ("inactive@example.test", "wrong", 401),
        ("unknown@example.test", PASSWORD, 401),
        ("inactive@example.test", PASSWORD, 403),
    ):
        assert client.post("/api/auth/login", json={"email": email, "password": password}).status_code == status
    for payload in (["email"], "not an object", True):
        assert client.post("/api/auth/login", json=payload).status_code == 400
    with client.session_transaction() as session:
        assert "_user_id" not in session
