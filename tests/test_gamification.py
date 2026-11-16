from datetime import date, datetime, timedelta
from pathlib import Path
import unittest

from __init__ import create_app
from extensions import db
from models import Appointment, User, XpEvent, utcnow
from routes import _balance_streak_from_events
from zoneinfo import ZoneInfo


def add_user(roles):
    user = User(display_name="Test User", email=f"{roles[0]}-{User.query.count()}@example.test")
    user.roles = roles
    user.set_password("test-password")
    db.session.add(user)
    db.session.flush()
    return user


def login(client, user_id):
    with client.session_transaction() as session:
        session["_user_id"] = str(user_id)
        session["_fresh"] = True


class GamificationTests(unittest.TestCase):
  def setUp(self):
    self.app = create_app({
        "TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite://",
        "SECRET_KEY": "test-secret-key-at-least-32-characters-long",
    })
    with self.app.app_context():
      db.create_all()
    self.client = self.app.test_client()

  def tearDown(self):
    with self.app.app_context():
      db.session.remove()
      db.drop_all()

  def test_completed_session_awards_student_xp_once(self):
    with self.app.app_context():
        student = add_user(["student"])
        provider = add_user(["mentor"])
        appointment = Appointment(
            student_id=student.id,
            provider_id=provider.id,
            role="mentor",
            topic="Math",
            start_time=utcnow() - timedelta(hours=2),
            duration_minutes=60,
            status="accepted",
        )
        db.session.add(appointment)
        db.session.commit()
        student_id, provider_id, appointment_id = student.id, provider.id, appointment.id

    provider_client = self.client
    login(provider_client, provider_id)
    wrong_side = provider_client.post(f"/api/bookings/{appointment_id}/confirm-attendance")
    assert wrong_side.status_code == 403
    response = provider_client.post(f"/api/bookings/{appointment_id}/complete")
    assert response.status_code == 200
    assert response.get_json()["status"] == "accepted"
    with self.app.app_context():
        assert XpEvent.query.count() == 0

    student_client = self.app.test_client()
    login(student_client, student_id)
    confirmed = student_client.post(f"/api/bookings/{appointment_id}/confirm-attendance")
    assert confirmed.status_code == 200
    assert confirmed.get_json()["status"] == "completed"
    with self.app.app_context():
        event = XpEvent.query.one()
        assert (event.user_id, event.appointment_id, event.path, event.points) == (
            student_id, appointment_id, "academic", 50
        )

    duplicate = student_client.post(f"/api/bookings/{appointment_id}/confirm-attendance")
    assert duplicate.status_code == 200
    with self.app.app_context():
        assert XpEvent.query.count() == 1


  def test_student_can_read_only_own_progress(self):
    with self.app.app_context():
      student = add_user(["student"])
      provider = add_user(["coach"])
      appointment = Appointment(
          student_id=student.id, provider_id=provider.id, role="coach", topic="Review",
          start_time=utcnow() - timedelta(hours=2), duration_minutes=60, status="completed",
      )
      db.session.add(appointment)
      db.session.flush()
      db.session.add(XpEvent(user_id=student.id, appointment_id=appointment.id, path="gaming", points=50))
      db.session.commit()
      student_id = student.id

    login(self.client, student_id)
    progress = self.client.get("/api/student/progress")
    self.assertEqual(progress.status_code, 200, progress.get_data(as_text=True))
    payload = progress.get_json()
    self.assertEqual(payload["total_xp"], payload["gaming_xp"])
    self.assertEqual(payload["total_xp"], 50)
    self.assertEqual(payload["academic_xp"], 0)
    self.assertEqual(payload["badges"], {"academic": False, "gaming": True, "combined": False})
    self.assertEqual(payload["timezone"], "Asia/Manila")
    self.assertEqual(payload["balance_streak_weeks"], 0)
    self.assertFalse(payload["current_week_balanced"])
    self.assertNotIn("email", payload)

  def test_attendance_dispute_holds_xp(self):
    with self.app.app_context():
      student = add_user(["student"])
      provider = add_user(["mentor"])
      appointment = Appointment(
          student_id=student.id, provider_id=provider.id, role="mentor", topic="Math",
          start_time=utcnow() - timedelta(hours=2), duration_minutes=60, status="accepted",
      )
      db.session.add(appointment)
      db.session.commit()
      student_id, provider_id, appointment_id = student.id, provider.id, appointment.id

    provider_client = self.app.test_client()
    login(provider_client, provider_id)
    dispute = provider_client.post(f"/api/bookings/{appointment_id}/dispute-attendance")
    self.assertEqual(dispute.status_code, 200)

    student_client = self.app.test_client()
    login(student_client, student_id)
    confirm = student_client.post(f"/api/bookings/{appointment_id}/confirm-attendance")
    self.assertEqual(confirm.status_code, 409)
    with self.app.app_context():
      self.assertEqual(XpEvent.query.count(), 0)
      self.assertIsNotNone(db.session.get(Appointment, appointment_id).attendance_disputed_at)


  def test_progress_requires_authenticated_student(self):
    with self.app.app_context():
        provider = add_user(["coach"])
        db.session.commit()
        provider_id = provider.id
    login(self.client, provider_id)
    self.assertEqual(self.client.get("/api/student/progress").status_code, 403)


  def test_dashboard_has_no_self_reported_xp_controls(self):
    html = (Path(__file__).parents[1] / "templates" / "student" / "student-dashboard.html").read_text(encoding="utf-8")
    js = (Path(__file__).parents[1] / "static" / "js" / "script.js").read_text(encoding="utf-8")
    self.assertNotIn("data-xp-amount", html)
    self.assertNotIn("gpResetProgress", html)
    self.assertIn("gpStreakCount", html)
    self.assertNotIn("l2_gamification_", js)
    self.assertIn("/api/student/progress", js)

  def test_balance_streak_counts_consecutive_manila_weeks_and_holds_current_streak(self):
    from types import SimpleNamespace

    tz = ZoneInfo("Asia/Manila")
    current = date(2026, 11, 16)
    def event(day, path):
      # Noon PHT, represented as naive UTC like the database.
      local = datetime.combine(day, datetime.min.time()).replace(hour=4)
      return SimpleNamespace(created_at=local, path=path)

    previous = current - timedelta(days=7)
    events = [event(previous, "academic"), event(previous, "gaming")]
    self.assertEqual(_balance_streak_from_events(events, current, tz), (1, False))
    events.extend([event(current, "academic"), event(current, "gaming")])
    self.assertEqual(_balance_streak_from_events(events, current, tz), (2, True))
    gap_events = [event(current - timedelta(days=14), "academic"), event(current - timedelta(days=14), "gaming")]
    self.assertEqual(_balance_streak_from_events(gap_events, current, tz), (0, False))

  def test_weekly_leaderboard_is_opt_in_and_hides_private_fields(self):
    with self.app.app_context():
      included = add_user(["student"])
      excluded = add_user(["student"])
      provider = add_user(["coach"])
      appt1 = Appointment(student_id=included.id, provider_id=provider.id, role="coach", topic="A", start_time=utcnow(), status="completed")
      appt2 = Appointment(student_id=excluded.id, provider_id=provider.id, role="coach", topic="B", start_time=utcnow(), status="completed")
      db.session.add_all([appt1, appt2])
      db.session.flush()
      db.session.add_all([
          XpEvent(user_id=included.id, appointment_id=appt1.id, path="gaming", points=50),
          XpEvent(user_id=excluded.id, appointment_id=appt2.id, path="gaming", points=50),
      ])
      db.session.commit()
      included_id, excluded_id = included.id, excluded.id
    with self.app.app_context():
      included_user = db.session.get(User, included_id)
      included_user.leaderboard_opt_in = True
      db.session.commit()

    client = self.app.test_client()
    login(client, excluded_id)
    response = client.get("/api/leaderboard/weekly")
    self.assertEqual(response.status_code, 200)
    board = response.get_json()
    self.assertFalse(board["opted_in"])
    self.assertEqual(len(board["entries"]), 1)
    self.assertEqual(board["entries"][0]["display_name"], "Test User")
    self.assertNotIn("email", board["entries"][0])

  def test_student_can_opt_in_and_out(self):
    with self.app.app_context():
      student = add_user(["student"])
      db.session.commit()
      student_id = student.id
    client = self.app.test_client()
    login(client, student_id)
    self.assertEqual(client.get("/leaderboard").status_code, 200)
    enabled = client.post("/api/student/leaderboard-preference", json={"opted_in": True})
    self.assertEqual(enabled.status_code, 200)
    self.assertTrue(enabled.get_json()["opted_in"])
    disabled = client.post("/api/student/leaderboard-preference", json={"opted_in": False})
    self.assertEqual(disabled.status_code, 200)
    self.assertFalse(disabled.get_json()["opted_in"])
