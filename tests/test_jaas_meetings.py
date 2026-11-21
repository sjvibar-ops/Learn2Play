import json
import re
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from __init__ import create_app
from extensions import db
from models import Appointment, RoleApplication, User


class JaasMeetingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.private_pem = cls.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("ascii")
        cls.public_key = cls.private_key.public_key()

    def setUp(self):
        self.app = create_app(dict(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            SQLALCHEMY_DATABASE_URI="sqlite://",
            JAAS_APP_ID="vpaas-magic-cookie-testapp",
            JAAS_API_KEY_ID="test-key-id",
            JAAS_PRIVATE_KEY=self.private_pem,
        ))
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

        self.provider = User(
            display_name="Assigned Provider", email="provider@example.test", password_hash="test",
            roles_json='["mentor"]',
        )
        self.student = User(
            display_name="Assigned Student", email="student@example.test", password_hash="test",
            roles_json='["student"]',
        )
        self.unrelated = User(
            display_name="Other Student", email="other@example.test", password_hash="test",
            roles_json='["student"]',
        )
        db.session.add_all([self.provider, self.student, self.unrelated])
        db.session.flush()
        self.appointment = Appointment(
            provider_id=self.provider.id,
            student_id=self.student.id,
            role="mentor",
            topic="Test session",
            start_time=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1),
            duration_minutes=60,
            status="accepted",
            meeting_room="l2-test-room",
        )
        db.session.add(self.appointment)
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _login_as(self, user):
        with self.client.session_transaction() as session:
            session["_user_id"] = str(user.id)
            session["_fresh"] = True

    def _meeting_token(self, user):
        self._login_as(user)
        response = self.client.get(f"/sessions/{self.appointment.id}/meeting")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        match = re.search(r"const participantToken = (\".*?\");", html)
        self.assertIsNotNone(match)
        return json.loads(match.group(1))

    def _decode(self, token):
        claims = jwt.decode(
            token,
            self.public_key,
            algorithms=["RS256"],
            audience="jitsi",
            issuer="chat",
        )
        return claims, jwt.get_unverified_header(token)

    def test_assigned_provider_receives_moderator_token_for_exact_room(self):
        generated_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with patch("routes.datetime", wraps=datetime) as clock:
            clock.now.return_value = generated_at
            token = self._meeting_token(self.provider)
        # The fixture uses a fixed clock, so check the signed timestamps explicitly.
        claims = jwt.decode(
            token, self.public_key, algorithms=["RS256"], audience="jitsi", issuer="chat",
            options={"verify_exp": False, "verify_nbf": False},
        )
        timestamp = int(generated_at.timestamp())
        self.assertEqual(claims["nbf"], timestamp - 10)
        self.assertEqual(claims["exp"], timestamp + 600)
        self.assertEqual(claims["context"]["features"], {
            "recording": "false",
            "livestreaming": "false",
            "transcription": "false",
            "outbound-call": "false",
        })
        header = jwt.get_unverified_header(token)
        self.assertEqual(header["alg"], "RS256")
        self.assertEqual(header["typ"], "JWT")
        self.assertEqual(header["kid"], "vpaas-magic-cookie-testapp/test-key-id")
        self.assertEqual(claims["sub"], "vpaas-magic-cookie-testapp")
        self.assertEqual(claims["room"], "l2-test-room")
        self.assertFalse(claims["context"]["room"]["regex"])
        self.assertEqual(claims["context"]["user"]["id"], str(self.provider.id))
        self.assertEqual(claims["context"]["user"]["name"], self.provider.display_name)
        self.assertEqual(claims["context"]["user"]["moderator"], "true")

    def test_assigned_student_receives_non_moderator_token(self):
        token = self._meeting_token(self.student)
        claims, _ = self._decode(token)
        self.assertEqual(claims["context"]["user"]["moderator"], "false")
        self.assertEqual(claims["room"], self.appointment.meeting_room)

    def test_unrelated_user_is_rejected(self):
        self._login_as(self.unrelated)
        self.assertEqual(self.client.get(f"/api/sessions/{self.appointment.id}/join").status_code, 404)
        self.assertEqual(self.client.get(f"/sessions/{self.appointment.id}/meeting").status_code, 404)

    def test_pending_booking_is_rejected(self):
        self.appointment.status = "pending"
        db.session.commit()
        self._login_as(self.student)
        self.assertEqual(self.client.get(f"/api/sessions/{self.appointment.id}/join").status_code, 403)
        self.assertEqual(self.client.get(f"/sessions/{self.appointment.id}/meeting").status_code, 403)

    def test_join_endpoint_returns_learn2play_meeting_page(self):
        for participant in (self.student, self.provider):
            with self.subTest(participant=participant.id):
                self._login_as(participant)
                response = self.client.get(f"/api/sessions/{self.appointment.id}/join")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()["url"], f"/sessions/{self.appointment.id}/meeting")

    def assert_meeting_denied(self, status):
        with patch("routes.create_jaas_participant_token") as signer:
            response = self.client.get(f"/api/sessions/{self.appointment.id}/join")
            self.assertEqual(response.status_code, status)
            self.assertNotIn("url", response.get_json())
            response = self.client.get(f"/sessions/{self.appointment.id}/meeting")
            self.assertEqual(response.status_code, status)
            signer.assert_not_called()

    def test_pending_and_rejected_assigned_providers_cannot_join(self):
        self.provider.roles = ["student"]
        application = RoleApplication(
            user_id=self.provider.id, role="mentor", status="pending",
            bio="Application for the academic teaching track.",
        )
        db.session.add(application)
        for status in ("pending", "rejected"):
            with self.subTest(status=status):
                application.status = status
                db.session.commit()
                self._login_as(self.provider)
                self.assert_meeting_denied(403)

    def test_assigned_provider_loses_access_when_approval_changes(self):
        self._meeting_token(self.provider)
        for roles in (["student"], ["student", "coach"]):
            with self.subTest(roles=roles):
                self.provider.roles = roles
                db.session.commit()
                self.assert_meeting_denied(403)

    def test_inactive_assigned_provider_cannot_join(self):
        self.provider.is_active = False
        db.session.commit()
        self._login_as(self.provider)
        with patch("routes.create_jaas_participant_token") as signer:
            api = self.client.get(f"/api/sessions/{self.appointment.id}/join",
                                  headers={"Accept": "application/json"})
            page = self.client.get(f"/sessions/{self.appointment.id}/meeting")
            for response in (api, page):
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.headers["Location"].startswith("/auth?"))
            signer.assert_not_called()

    def test_payment_or_room_required_for_both_entry_points(self):
        self._login_as(self.provider)
        self.appointment.payment_required = True
        db.session.commit()
        self.assert_meeting_denied(403)
        self.appointment.payment_required = False
        self.appointment.meeting_room = None
        db.session.commit()
        self.assert_meeting_denied(403)

    def test_missing_jaas_configuration_fails_safely(self):
        self.app.config["JAAS_PRIVATE_KEY"] = ""
        self._login_as(self.student)
        response = self.client.get(f"/sessions/{self.appointment.id}/meeting")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.get_json()["message"],
            "Video meetings are temporarily unavailable. Please contact support.",
        )
        self.assertNotIn(self.private_pem, response.get_data(as_text=True))

    def test_complete_key_id_is_not_prefixed_twice(self):
        self.app.config["JAAS_API_KEY_ID"] = "vpaas-magic-cookie-testapp/existing-key-id"
        claims, header = self._decode(self._meeting_token(self.provider))
        self.assertEqual(header["kid"], "vpaas-magic-cookie-testapp/existing-key-id")
        self.assertEqual(claims["context"]["user"]["moderator"], "true")


if __name__ == "__main__":
    unittest.main()
