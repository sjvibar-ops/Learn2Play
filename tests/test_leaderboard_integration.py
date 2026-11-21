"""Leaderboard UI/API integration against isolated SQLite and a Node DOM harness."""
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess
from unittest.mock import patch

import pytest
from __init__ import create_app
from extensions import db
from models import Appointment, User, XpEvent


@pytest.fixture
def app():
    app = create_app(dict(TESTING=True, WTF_CSRF_ENABLED=False,
                          SECRET_KEY="isolated-leaderboard-test-key",
                          SQLALCHEMY_DATABASE_URI="sqlite://", SQLALCHEMY_ENGINE_OPTIONS={}))
    with app.app_context():
        db.create_all()
    with patch("routes.datetime", wraps=datetime) as clock:
        clock.now.return_value = datetime(2026, 11, 18, 4, tzinfo=timezone.utc)
        yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


def user(name, roles=("student",), opted=True, **extra):
    row = User(display_name=name, email=f"user-{User.query.count()}@example.test",
               password_hash="isolated", leaderboard_opt_in=opted, **extra)
    row.roles = list(roles)
    db.session.add(row)
    db.session.flush()
    return row


def event(student, provider, points=50, path="academic", at=None):
    appointment = Appointment(student_id=student.id, provider_id=provider.id,
                              topic="Test XP", role="mentor", status="completed",
                              start_time=datetime(2026, 11, 17))
    db.session.add(appointment)
    db.session.flush()
    db.session.add(XpEvent(user_id=student.id, appointment_id=appointment.id,
                          path=path, points=points, created_at=at or datetime(2026, 11, 17)))


def client_for(app, uid):
    client = app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = str(uid)
        session["_fresh"] = True
    return client


def test_own_rank_below_top_50_is_private_and_public_list_stays_bounded(app):
    with app.app_context():
        provider = user("Provider", ("mentor",), opted=False)
        students = [user(f"Learner {i:02}") for i in range(52)]
        for i, student in enumerate(students):
            event(student, provider, points=(52-i)*50)
        db.session.commit()
        last, first = students[-1].id, students[0].id
    response = client_for(app, last).get("/api/leaderboard/weekly?user_id=" + str(first))
    assert response.status_code == 200
    data = response.json
    assert len(data["entries"]) == 50
    assert data["entries"][0] == {"rank": 1, "display_name": "Learner 00", "weekly_xp": 2600}
    assert data["your_progress"] == {"weekly_xp": 50, "rank": 52}
    assert all(set(entry) == {"rank", "display_name", "weekly_xp"} for entry in data["entries"])
    assert "no-store" in response.headers["Cache-Control"]


def test_ties_week_boundaries_and_opt_out_preserve_ranking_rules(app):
    with app.app_context():
        provider = user("Provider", ("mentor",), opted=False)
        second = user("Beta")
        first = user("Alpha")
        excluded = user("Private", opted=False)
        inactive = user("Inactive", is_active=False)
        admin = user("Admin", is_admin=True)
        for student in (first, second, excluded, inactive, admin):
            event(student, provider)
        # Monday 00:00 PHT is Sunday 16:00 UTC. End is exclusive.
        event(first, provider, at=datetime(2026, 11, 15, 16))
        event(first, provider, at=datetime(2026, 11, 15, 15, 59, 59))
        event(first, provider, at=datetime(2026, 11, 22, 16))
        db.session.commit()
        uid = first.id
    client = client_for(app, uid)
    data = client.get("/api/leaderboard/weekly").json
    assert data["week_start"] == "2026-11-16"
    assert data["week_end"] == "2026-11-22"
    assert data["entries"] == [
        {"rank": 1, "display_name": "Alpha", "weekly_xp": 100},
        {"rank": 2, "display_name": "Beta", "weekly_xp": 50},
    ]
    assert client.post("/api/student/leaderboard-preference", json={"opted_in": False}).json["opted_in"] is False
    after = client.get("/api/leaderboard/weekly").json
    assert after["your_progress"] is None
    assert [entry["display_name"] for entry in after["entries"]] == ["Beta"]


def test_equal_xp_order_is_stable_and_zero_xp_has_no_rank(app):
    with app.app_context():
        provider = user("Provider", ("coach",), opted=False)
        beta, alpha, none = user("Beta"), user("Alpha"), user("No XP")
        event(beta, provider)
        event(alpha, provider)
        db.session.commit()
        uid = none.id
    data = client_for(app, uid).get("/api/leaderboard/weekly").json
    assert [row["display_name"] for row in data["entries"]] == ["Alpha", "Beta"]
    assert data["your_progress"] == {"weekly_xp": 0, "rank": None}


@pytest.mark.parametrize("roles,admin", [(("mentor",), False), (("coach",), False), (("student",), True)])
def test_provider_and_admin_authorization_remains_enforced(app, roles, admin):
    with app.app_context():
        actor = user("Restricted", roles, is_admin=admin)
        db.session.commit()
        uid = actor.id
    client = client_for(app, uid)
    for path in ("/leaderboard", "/api/leaderboard/weekly", "/api/student/progress"):
        assert client.get(path).status_code == 403
    assert client.post("/api/student/leaderboard-preference", json={"opted_in": True}).status_code == 403


def test_preference_is_csrf_protected_and_cannot_edit_another_user_or_xp(app):
    with app.app_context():
        actor, other = user("Student", opted=False), user("Other", opted=False)
        db.session.commit()
        uid, other_id = actor.id, other.id
    client = client_for(app, uid)
    response = client.post("/api/student/leaderboard-preference", json={
        "opted_in": True, "user_id": other_id, "weekly_xp": 999999, "rank": 1})
    assert response.status_code == 200
    assert client.get("/api/leaderboard/weekly").json["your_progress"] == {"weekly_xp": 0, "rank": None}
    with app.app_context():
        assert db.session.get(User, uid).leaderboard_opt_in is True
        assert db.session.get(User, other_id).leaderboard_opt_in is False
        assert XpEvent.query.count() == 0
    app.config["WTF_CSRF_ENABLED"] = True
    assert client.post("/api/student/leaderboard-preference", json={"opted_in": False}).status_code == 400


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids, self.scripts, self.links = [], [], []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "script" and "src" in attrs:
            self.scripts.append(attrs["src"])
        if tag == "link" and attrs.get("rel") == "stylesheet":
            self.links.append(attrs["href"])


def test_page_assets_dashboard_navigation_and_unchanged_progress(app):
    with app.app_context():
        provider = user("Mentor", ("mentor",), opted=False)
        actor = user("Student")
        event(actor, provider, path="academic")
        event(actor, provider, path="gaming")
        db.session.commit()
        uid = actor.id
    client = client_for(app, uid)
    response = client.get("/leaderboard")
    assert response.status_code == 200
    page = Page()
    page.feed(response.get_data(as_text=True))
    assert len(page.ids) == len(set(page.ids))
    for name in ("leaderboardPodium", "leaderboardRetry", "leaderboardProgressStats", "leaderboardChallengeTrack"):
        assert name in page.ids
    for asset in page.scripts + page.links:
        assert client.get(asset).status_code == 200
    assert page.scripts.count("/static/js/modes.js") == 1
    dashboard = client.get("/dashboard").get_data(as_text=True)
    assert 'href="/leaderboard"' in dashboard
    progress = client.get("/api/student/progress").json
    assert progress["total_xp"] == 100
    assert progress["level"] == 2
    assert progress["quests"]["balanced_week"] == {"count": 2, "target": 2}
    assert progress["badges"] == {"academic": True, "gaming": True, "combined": True}
    assert app.test_client().get("/leaderboard").status_code == 302
    assert app.test_client().get("/api/leaderboard/weekly").status_code == 302


def test_leaderboard_browser_logic():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js unavailable")
    subprocess.run([node, str(Path(__file__).with_name("leaderboard_podium.cjs"))], check=True, timeout=30)
