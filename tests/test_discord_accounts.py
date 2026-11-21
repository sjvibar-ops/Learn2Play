from urllib.parse import parse_qs, urlparse

import pytest
from flask_migrate import upgrade

import discord_oauth
from __init__ import create_app
from extensions import db
from models import DiscordAccount, User


IDENTITY = "123456789012345678"


@pytest.fixture
def linked_app(monkeypatch):
    def reject_live_http(*args, **kwargs):
        pytest.fail("Discord tests must use fake HTTP")

    monkeypatch.setattr(discord_oauth, "urlopen", reject_live_http)
    app = create_app({
        "TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite://",
        "DISCORD_LINKING_ENABLED": True, "DISCORD_CLIENT_ID": "test-client",
        "DISCORD_CLIENT_SECRET": "test-secret", "DISCORD_REDIRECT_URI": "http://localhost/callback",
        "DISCORD_GUILD_ID": IDENTITY, "DISCORD_BOT_TOKEN": "test-bot",
    })
    with app.app_context():
        upgrade()
        for index in (1, 2):
            user = User(id=index, email=f"user{index}@example.test", display_name="Student")
            user.set_password("Disposable1")
            user.roles = ["student"]
            db.session.add(user)
        db.session.commit()
    return app


def signed_in(app, user_id):
    client = app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = str(user_id)
    return client


def start(client):
    response = client.post("/api/me/discord/connect")
    assert response.status_code == 303
    query = parse_qs(urlparse(response.location).query)
    assert query["scope"] == ["identify"]
    assert "client_secret" not in query
    return query["state"][0]


def finish(client, state, code="mock-code"):
    return client.get("/api/me/discord/callback", query_string={"state": state, "code": code})


def test_link_conflict_membership_and_unlink(linked_app, monkeypatch):
    monkeypatch.setattr(discord_oauth, "fetch_identity", lambda *_: {"id": IDENTITY})
    monkeypatch.setattr(discord_oauth, "_bot_http_request",
                        lambda *_: {"status": 404, "payload": {"code": 10007}})
    first, second = signed_in(linked_app, 1), signed_in(linked_app, 2)
    state = start(first)
    assert finish(first, state).status_code == 302
    assert finish(first, state).status_code == 400
    assert finish(second, start(second)).status_code == 409
    assert first.get("/api/me/discord/guild-status").json["is_member"] is False
    with linked_app.app_context():
        assert db.session.get(DiscordAccount, 1).discord_id == IDENTITY
        assert db.session.get(User, 1).roles == ["student"]
    assert first.post("/api/me/discord/disconnect").json["linked"] is False
    assert first.get("/api/me/discord/guild-status").json["status"] == "NOT_LINKED"


def test_invalid_expired_and_denied_callback(linked_app):
    client = signed_in(linked_app, 1)
    assert finish(client, "bad").status_code == 400
    state = start(client)
    with client.session_transaction() as session:
        session[discord_oauth.SESSION_KEY] = {
            **session[discord_oauth.SESSION_KEY], "expires_at": 0,
        }
    assert finish(client, state).status_code == 400
    state = start(client)
    assert client.get("/api/me/discord/callback", query_string={"state": state, "error": "access_denied"}).status_code == 400


def test_authentication_feature_flag_and_redacted_errors(linked_app, monkeypatch, caplog):
    assert linked_app.test_client().post("/api/me/discord/connect").status_code in (302, 401)
    client = signed_in(linked_app, 1)

    def failed_exchange(*_):
        raise discord_oauth.DiscordOAuthError("mock-sensitive-response")

    monkeypatch.setattr(discord_oauth, "fetch_identity", failed_exchange)
    response = finish(client, start(client))
    assert response.status_code == 503
    assert "mock-sensitive-response" not in response.get_data(as_text=True) + caplog.text
    assert response.headers["Referrer-Policy"] == "no-referrer"
    linked_app.config["DISCORD_LINKING_ENABLED"] = False
    assert client.post("/api/me/discord/connect").status_code == 404
