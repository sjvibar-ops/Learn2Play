import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from __init__ import create_app
from extensions import db
from models import Appointment, MentorAvailability, User


@pytest.fixture
def app():
    app = create_app(dict(
        TESTING=True,
        SECRET_KEY="isolated-ui-integration-test-key",
        WTF_CSRF_ENABLED=False,
        SQLALCHEMY_DATABASE_URI="sqlite://",
    ))
    with app.app_context():
        db.create_all()
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.mark.parametrize("roles", [
    ["student"],
    ["mentor"],
    ["coach"],
    ["student", "mentor", "coach"],
])
def test_dashboard_and_direct_access_follow_flask_roles(app, roles):
    with app.app_context():
        user = User(display_name="Role Test", email="roles@example.test")
        user.set_password("StrongPassword1")
        user.roles = roles
        db.session.add(user)
        db.session.commit()
        user_id = user.id

    client = app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = str(user_id)
        session["_fresh"] = True

    assert set(client.get("/api/me").json["user"]["roles"]) == set(roles)
    provider = any(role in roles for role in ("mentor", "coach"))
    for path in ("/dashboard", "/dashboard?mode=coach", "/dashboard?mode=mentor"):
        page = client.get(path)
        assert page.status_code == 200
        assert (b'id="gamificationPreview"' in page.data) == ("student" in roles)
        assert (b'id="mentorHome"' in page.data) == provider
        for role in ("student", "mentor", "coach"):
            assert (f'data-mode="{role}"'.encode() in page.data) == (role in roles)

    directory = client.get("/dashboard?view=browse")
    assert b'id="tutorGrid"' in directory.data
    assert b'id="gamificationPreview"' not in directory.data
    assert b'id="mentorHome"' not in directory.data
    expected_status = 200 if provider else 403
    assert client.get("/schedule").status_code == expected_status
    assert client.get("/api/availability").status_code == expected_status
    if provider:
        profile = client.get("/profile")
        assert profile.status_code == 200
        assert (b'id="editTeachingRate"' in profile.data) == ("mentor" in roles)
        assert (b'id="editCoachingRate"' in profile.data) == ("coach" in roles)


def test_anonymous_user_cannot_access_dashboard_or_schedule(app):
    client = app.test_client()
    assert client.get("/dashboard?mode=coach").status_code == 302
    assert client.get("/schedule").status_code == 302
    assert client.get("/api/me").status_code == 401


def test_student_can_book_an_offered_track_and_educator_can_accept(app):
    day = date.today() + timedelta(days=2)
    with app.app_context():
        learner = User(display_name="Learner", email="learner@example.test")
        learner.roles = ["student"]
        coach = User(display_name="Coach", email="coach@example.test", coaching_rate=800)
        coach.roles = ["coach"]
        for user in (learner, coach):
            user.set_password("StrongPassword1")
        db.session.add_all([learner, coach])
        db.session.flush()
        learner_id, coach_id = learner.id, coach.id
        db.session.add(MentorAvailability(
            mentor_id=coach_id, weekday=day.weekday(), start_time="09:00", end_time="12:00"
        ))
        db.session.commit()

    student_client, coach_client = app.test_client(), app.test_client()
    for client, user_id in ((student_client, learner_id), (coach_client, coach_id)):
        with client.session_transaction() as session:
            session["_user_id"] = str(user_id)
            session["_fresh"] = True

    path = f"/tutors/{coach_id}/book"
    assert b'id="bookingPage"' in student_client.get(path).data
    assert coach_client.get(path).status_code == 302

    request_data = {"mentor_id": coach_id, "date": day.isoformat(), "time": "09:00", "topic": "Strategy"}
    assert coach_client.post("/api/bookings", json=dict(request_data, role="coach")).status_code == 403
    invalid = student_client.post("/api/bookings", json=dict(request_data, role="mentor"))
    assert invalid.status_code == 400
    assert "does not offer" in invalid.json["message"]

    booking = student_client.post("/api/bookings", json=dict(request_data, role="coach"))
    assert booking.status_code == 201
    accept = f"/api/bookings/{booking.json['booking_id']}/accept"
    assert student_client.post(accept).status_code == 403
    assert coach_client.post(accept).json["status"] == "accepted"
    session = student_client.get("/api/bookings").json["bookings"][0]
    assert (session["role"], session["price"], session["status"]) == ("coach", 800, "accepted")


def test_landing_progress_link_has_a_destination(app):
    landing = app.test_client().get("/")
    assert landing.status_code == 200
    assert b'href="#landing-features-title"' in landing.data
    assert b'id="landing-features-title"' in landing.data
    assert b"gamification-preview.html" not in landing.data


def test_booking_api_serializes_existing_utc_time_without_shifting_it(app):
    stored_start = datetime(2026, 11, 15, 16, 0)
    with app.app_context():
        learner = User(display_name="Learner", email="timezone-learner@example.test")
        learner.roles = ["student"]
        coach = User(display_name="Coach", email="timezone-coach@example.test")
        coach.roles = ["coach"]
        for user in (learner, coach):
            user.set_password("StrongPassword1")
        db.session.add_all([learner, coach])
        db.session.flush()
        appointment = Appointment(
            student_id=learner.id, provider_id=coach.id, role="coach",
            topic="Philippine midnight", start_time=stored_start, status="accepted",
        )
        db.session.add(appointment)
        db.session.commit()
        learner_id, appointment_id = learner.id, appointment.id

    client = app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = str(learner_id)
        session["_fresh"] = True

    response = client.get("/api/bookings")
    assert response.status_code == 200
    booking = response.json["bookings"][0]
    assert booking["start_time"] == "2026-11-15T16:00:00+00:00"
    assert datetime.fromisoformat(booking["start_time"]) == stored_start.replace(tzinfo=timezone.utc)
    assert "Nov 16" in booking["formatted"]
    with app.app_context():
        assert db.session.get(Appointment, appointment_id).start_time == stored_start


def test_booking_timezone_javascript():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the booking timezone tests")
    subprocess.run([node, str(Path(__file__).with_name("booking_timezone.cjs"))], check=True)


def test_in_progress_confirmed_booking_is_joinable_by_participants(app, monkeypatch):
    start = datetime(2026, 11, 15, 16)
    monkeypatch.setattr("routes.utcnow", lambda: start + timedelta(seconds=1))
    with app.app_context():
        learner = User(display_name="Learner", email="join-learner@example.test")
        learner.roles = ["student"]
        coach = User(display_name="Coach", email="join-coach@example.test")
        coach.roles = ["coach"]
        outsider = User(display_name="Outsider", email="join-outsider@example.test")
        outsider.roles = ["student"]
        for user in (learner, coach, outsider):
            user.set_password("StrongPassword1")
        db.session.add_all([learner, coach, outsider])
        db.session.flush()
        booking = Appointment(
            student_id=learner.id, provider_id=coach.id, role="coach", topic="In progress",
            start_time=start, duration_minutes=30, status="accepted", meeting_room="test-in-progress",
        )
        db.session.add(booking)
        db.session.commit()
        path = f"/api/sessions/{booking.id}/join"
        meeting_path = f"/sessions/{booking.id}/meeting"
        learner_id, coach_id, outsider_id = learner.id, coach.id, outsider.id

    for user_id, status in [(learner_id, 200), (coach_id, 200), (outsider_id, 404)]:
        client = app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(user_id)
            session["_fresh"] = True
        response = client.get(path)
        assert response.status_code == status
        if status == 200:
            assert response.json["url"] == meeting_path


def test_mode_switch_and_directory_javascript():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the UI script tests")
    subprocess.run([node, str(Path(__file__).with_name("ui_integration.cjs"))], check=True)
