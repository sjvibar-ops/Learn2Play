from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta
from threading import Barrier

import pytest
from flask_migrate import upgrade

import discord_oauth as oauth
from __init__ import create_app
from extensions import db
from models import (
    Appointment, DiscordAccount, DiscordRoom, DiscordRoomEvent, MentorAvailability,
    MentorUnavailableDate, Payment, RescheduleProposal, User, utcnow,
)


def test_evidence_storage_fails_closed_for_public_or_unknown_bucket(monkeypatch):
    from unittest.mock import Mock

    import routes

    storage_client = Mock()
    monkeypatch.setenv("SUPABASE_URL", "https://storage.example.invalid")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fixture-only")
    monkeypatch.setattr(routes, "create_client", lambda *_: storage_client)
    for metadata in ({"public": True}, {}):
        storage_client.storage.get_bucket.return_value = metadata
        with pytest.raises(RuntimeError, match="must be private"):
            routes.credentials_bucket()
    storage_client.storage.from_.assert_not_called()
    storage_client.storage.get_bucket.return_value = {"public": False}
    assert routes.credentials_bucket() == storage_client.storage.from_.return_value

GUILD = "123456789012345600"
STUDENT = "123456789012345601"
PROVIDER = "123456789012345602"
CHANNEL = "123456789012345603"
DISCORD_PATH = "/api/sessions/1/discord-channel"
ADMIN_PATH = "/api/admin/discord/rooms/1"
RESCHEDULE_PATH = "/api/bookings/1/reschedule"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        pytest.fail("All Discord requests must be mocked")

    monkeypatch.setattr(oauth, "urlopen", no_network)
    app = create_app({
        "TESTING": True, "WTF_CSRF_ENABLED": False,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'coordination.db'}",
        "DISCORD_SESSIONS_ENABLED": True, "DISCORD_GUILD_ID": GUILD, "DISCORD_BOT_TOKEN": "fake-bot",
        "PAYMENT_DEMO": False,
    })
    day = (utcnow() + timedelta(days=3)).date()
    start = datetime.combine(day, time(2))
    with app.app_context():
        upgrade()
        db.session.add_all([
            User(id=1, display_name="Student", email="a@example.test", password_hash="fixture", roles_json='["student"]'),
            User(id=2, display_name="Coach", email="b@example.test", password_hash="fixture", roles_json='["coach"]'),
            User(id=3, display_name="Outsider", email="c@example.test", password_hash="fixture"),
            User(id=4, display_name="Admin", email="d@example.test", password_hash="fixture", is_admin=True),
        ])
        db.session.flush()
        db.session.add_all([
            DiscordAccount(user_id=1, discord_id=STUDENT),
            DiscordAccount(user_id=2, discord_id=PROVIDER),
            MentorAvailability(mentor_id=2, weekday=day.weekday(), start_time="09:00", end_time="17:00"),
            Appointment(id=1, student_id=1, provider_id=2, role="coach", topic="Fixture",
                        start_time=start, status="accepted", meeting_room="fixture-room", payment_required=True),
        ])
        db.session.flush()
        db.session.add(Payment(id=1, appointment_id=1, student_id=1, reference="fixture", amount=100, status="VERIFIED"))
        db.session.commit()
    return app, day, start


def client(app, user):
    browser = app.test_client()
    with browser.session_transaction() as session:
        session["_user_id"] = str(user)
    return browser


def propose(browser, day, time="11:00"):
    return browser.post(RESCHEDULE_PATH, json={"action":"propose", "date": day.isoformat(), "time":time})


@pytest.mark.parametrize("requester,decider", [(1,2),(2,1)])
def test_mutual_reschedule_keeps_original_payment_and_audit(setup, requester, decider):
    app, day, original = setup
    initiator, recipient = client(app, requester), client(app, decider)
    response = propose(initiator, day)
    assert response.status_code == 201
    proposal_id = response.json["proposal"]["id"]
    decision = {"action":"accept", "proposal_id":proposal_id}
    assert initiator.post(RESCHEDULE_PATH, json=decision).status_code == 403
    assert propose(initiator, day).status_code == 409
    with app.app_context():
        assert db.session.get(Appointment, 1).start_time == original
    assert recipient.post(RESCHEDULE_PATH, json=decision).status_code == 200
    assert recipient.post(RESCHEDULE_PATH, json=decision).status_code == 409
    with app.app_context():
        booking, payment = db.session.get(Appointment, 1), db.session.get(Payment, 1)
        proposal = db.session.get(RescheduleProposal, proposal_id)
        assert booking.start_time == original + timedelta(hours=1)
        assert (booking.meeting_room, payment.appointment_id, payment.reference, payment.status) == (
            "fixture-room", 1, "fixture", "VERIFIED")
        assert (proposal.requested_by, proposal.decided_by, proposal.previous_start) == (requester, decider, original)


def test_rescheduling_validates_ownership_time_availability_and_conflicts(setup):
    app, day, original = setup
    s, p = client(app, 1), client(app, 2)
    assert propose(client(app, 3), day).status_code == 404
    assert propose(s, day, "11:30").status_code == 400
    assert propose(s, day, "18:00").status_code == 409
    with app.app_context():
        db.session.add(MentorUnavailableDate(mentor_id=2, unavailable_date=day))
        db.session.commit()
    assert propose(s, day).status_code == 409
    with app.app_context():
        db.session.delete(MentorUnavailableDate.query.one())
        db.session.commit()
    response = propose(s, day)
    assert response.status_code == 201
    proposal_id = response.json["proposal"]["id"]
    with app.app_context():
        db.session.add(Appointment(student_id=3, provider_id=2, role="coach", topic="Conflict",
                                   start_time=original + timedelta(hours=1), status="pending"))
        db.session.commit()
    assert p.post(RESCHEDULE_PATH,json={"action":"accept","proposal_id":proposal_id}).status_code == 409
    assert p.post(RESCHEDULE_PATH,json={"action":"reject","proposal_id":proposal_id}).status_code == 200
    assert s.get(RESCHEDULE_PATH).json["history"][0]["status"] == "rejected"


@pytest.mark.parametrize("status", ["cancelled", "completed", "disputed"])
def test_terminal_and_disputed_appointments_cannot_move(setup, status):
    app, day, _ = setup
    with app.app_context():
        appointment = db.session.get(Appointment, 1)
        if status == "disputed":
            appointment.attendance_disputed_at = utcnow()
        else:
            appointment.status = status
        db.session.commit()
    assert propose(client(app, 1), day).status_code == 409


def test_simultaneous_proposals_create_one_record(setup):
    app, day, _ = setup
    barrier = Barrier(2)

    def submit(actor):
        browser = client(app, actor)
        barrier.wait(timeout=5)
        return propose(browser, day).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(submit, (1, 2))) == [201, 409]
    with app.app_context():
        assert RescheduleProposal.query.count() == 1


def channel_object(marker):
    return {"id": CHANNEL, "name": marker, "guild_id": GUILD, "type": 2,
            "permission_overwrites": oauth.build_private_voice_overwrites(GUILD, STUDENT, PROVIDER)}


def test_discord_provision_checks_eligibility_and_reuses_room(setup, monkeypatch):
    app, _, _ = setup
    calls = []
    monkeypatch.setattr(oauth, "check_guild_membership", lambda *_: {"is_member": True})

    def rest(method, url, token, body=None):
        calls.append(method)
        return {"status": 201, "payload": channel_object(body["name"])}

    monkeypatch.setattr(oauth, "_bot_api_request", rest)
    assert client(app, 3).post(DISCORD_PATH).status_code == 404
    with app.app_context():
        db.session.get(Payment, 1).status = "SUBMITTED"
        db.session.commit()
    assert client(app, 1).post(DISCORD_PATH).status_code == 403
    with app.app_context():
        db.session.get(Payment, 1).status = "VERIFIED"
        db.session.commit()
    app.config["PAYMENT_DEMO"] = True
    assert client(app, 1).post(DISCORD_PATH).status_code == 403
    app.config["PAYMENT_DEMO"] = False
    assert calls == []
    first = client(app, 1).post(DISCORD_PATH)
    second = client(app, 2).post(DISCORD_PATH)
    assert first.status_code == second.status_code == 200
    assert first.json["url"] == second.json["url"]
    assert calls == ["POST"]


def test_ambiguous_create_can_be_recovered_and_cleanup_is_idempotent(setup, monkeypatch):
    app, _, _ = setup
    student, admin = client(app, 1), client(app, 4)
    calls = []
    monkeypatch.setattr(oauth, "check_guild_membership", lambda *_: {"is_member": True})
    marker = None
    channel_name_matches = False

    def rest(method, url, token, body=None):
        nonlocal marker
        calls.append(method)
        if method == "POST":
            marker = body["name"]
            return {"status": 502, "payload": None}
        if method == "GET":
            return {"status": 200, "payload": channel_object(marker if channel_name_matches else "unrelated")}
        return {"status": 204, "payload": None}

    monkeypatch.setattr(oauth, "_bot_api_request", rest)
    assert student.post(DISCORD_PATH).status_code == 503
    assert student.post(DISCORD_PATH).status_code == 409
    assert student.post(ADMIN_PATH + "/cleanup").status_code == 403
    assert admin.post(ADMIN_PATH + "/recover", json={"channel_id": CHANNEL}).status_code == 409
    channel_name_matches = True
    assert admin.post(ADMIN_PATH + "/recover", json={"channel_id": CHANNEL}).status_code == 200
    assert admin.post(ADMIN_PATH + "/cleanup").status_code == 200
    assert admin.post(ADMIN_PATH + "/cleanup").status_code == 200
    assert calls == ["POST", "GET", "GET", "GET", "DELETE"]
    with app.app_context():
        assert [row.action for row in DiscordRoomEvent.query.order_by(DiscordRoomEvent.id)] == [
            "provisioning", "recovered", "removed"]


def test_rate_limit_and_already_missing_channel(setup, monkeypatch):
    app, _, _ = setup
    monkeypatch.setattr(oauth, "check_guild_membership", lambda *_: {"is_member": True})
    monkeypatch.setattr(oauth, "_bot_api_request", lambda *_: {"status": 429, "payload": None, "retry_after": 3})
    response = client(app, 2).post(DISCORD_PATH)
    assert response.status_code == 503 and int(response.headers["Retry-After"]) >= 3
    with app.app_context():
        room = db.session.get(DiscordRoom, 1)
        assert room.state == "failed"
        room.state, room.channel_id = "ready", CHANNEL
        db.session.commit()
    monkeypatch.setattr(oauth, "_bot_api_request", lambda *_: {"status": 404, "payload": {"code":10003}})
    assert client(app, 4).post(ADMIN_PATH + "/cleanup").json["state"] == "removed"
