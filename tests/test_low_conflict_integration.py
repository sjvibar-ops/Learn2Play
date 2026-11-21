import unittest
from flask import g
from __init__ import create_app
from extensions import db
from models import Appointment, User
from datetime import datetime


class LowConflictIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(dict(TESTING=True, SECRET_KEY="isolated-low-conflict-test-key",
                                   SQLALCHEMY_DATABASE_URI="sqlite:///:memory:", WTF_CSRF_ENABLED=True))
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.student = User(display_name="Learner", email="learner@example.test")
        self.student.roles = ["student"]
        self.provider = User(display_name="Teacher", email="teacher@example.test")
        self.provider.roles = ["mentor"]
        for user in (self.student, self.provider):
            user.set_password("StrongPassword1")
        db.session.add_all([self.student, self.provider])
        db.session.flush()
        db.session.add(Appointment(student_id=self.student.id, provider_id=self.provider.id,
                                   topic="Math", start_time=datetime(2020, 1, 1),
                                   status="completed", duration_minutes=60))
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def login(self, user):
        g.pop("_login_user", None)
        with self.client.session_transaction() as session:
            session["_user_id"] = str(user.id)
            session["_fresh"] = True

    def test_completed_sessions_do_not_create_ratings(self):
        self.login(self.student)
        stats = self.client.get("/api/me").json["user"]["stats"]
        self.assertEqual(stats["sessions_count"], 1)
        self.assertIsNone(stats["avg_rating"])
        tutors = self.client.get("/api/tutors").json["tutors"]
        self.assertTrue(tutors)
        for tutor in tutors:
            self.assertIsNone(tutor["rating"])
            self.assertEqual(tutor["reviews"], 0)

    def test_auth_controls_and_no_fictional_review_counts(self):
        html = self.client.get("/auth").get_data(as_text=True)
        self.assertIn('aria-controls="password" aria-pressed="false"', html)
        self.assertIn('id="note" role="status" aria-live="polite"', html)
        self.assertIn('name="csrf-token"', html)
        self.assertNotIn('class="rating"', html)
        self.assertNotIn('(143)', html)
        self.assertNotIn('(211)', html)

    def test_existing_role_validation_rejects_privilege_escalation(self):
        for roles in (["admin"], ["student", "owner"], "mentor", [1], None):
            with self.subTest(roles=roles), self.assertRaises(ValueError):
                self.student.roles = roles
        self.assertEqual(self.student.roles, ["student"])
        self.student.roles = ["student", "student", "mentor"]
        self.assertEqual(self.student.roles, ["student", "mentor"])

    def test_navigation_and_role_pages_still_render(self):
        self.login(self.student)
        for path in ("/dashboard", "/dashboard?view=browse", "/sessions", "/profile", "/settings"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)
        self.assertIn(b"leaderboard", self.client.get("/dashboard").data)
        self.login(self.provider)
        for path in ("/dashboard", "/schedule", "/sessions", "/profile"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)
