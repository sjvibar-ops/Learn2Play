"""Batch 2.1A: registration without evidence, limited-access login, and
role-specific booking authorization.

Every database test builds its app through create_app(test_config) with the
verified in-memory SQLite URI, so no configured database can be opened.
Supabase Storage is never reached: the credential-storage entry points in the
routes module are replaced with mocks that fail the test if they are called.
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from unittest import mock
from flask import g, request_started

try:
    from Backend import create_app
    from Backend.extensions import db
    from Backend.models import Appointment, Credential, RoleApplication, User
except ImportError:
    from __init__ import create_app
    from extensions import db
    from models import Appointment, Credential, RoleApplication, User


PASSWORD = "Passw0rdOK"


def naive_utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app({
            "TESTING": True,
            "WTF_CSRF_ENABLED": False,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        })
        self.routes = sys.modules[self.app.view_functions["main.register"].__module__]

        # Fail loudly if anything under test reaches Supabase Storage.
        patchers = [
            mock.patch.object(
                self.routes, "credentials_bucket",
                side_effect=AssertionError("Supabase Storage must not be used"),
            ),
            mock.patch.object(
                self.routes, "create_client",
                side_effect=AssertionError("Supabase client must not be created"),
            ),
        ]
        self.bucket_mock, self.client_mock = [p.start() for p in patchers]
        for p in patchers:
            self.addCleanup(p.stop)

        def reset_identity(sender, **extra):
            g.pop("_login_user", None)
        request_started.connect(reset_identity, self.app, weak=False)
        self.addCleanup(request_started.disconnect, reset_identity, self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    # ----- helpers -----
    def make_user(self, email, roles, active=True, password=PASSWORD, name=None):
        user = User(display_name=name or email.split("@")[0], email=email, is_active=active)
        user.set_password(password)
        user.roles = roles
        db.session.add(user)
        db.session.commit()
        return user

    def add_application(self, user, role, status):
        row = RoleApplication(user_id=user.id, role=role, bio="x" * 60, links="", status=status)
        db.session.add(row)
        db.session.commit()
        return row

    def login(self, client, email, password=PASSWORD):
        return client.post("/api/auth/login", json={"email": email, "password": password})

    def registration_form(self, **overrides):
        data = {
            "name": "Maria Santos",
            "email": "maria@example.test",
            "password": PASSWORD,
            "confirm_password": PASSWORD,
            "acknowledged": "true",
        }
        data.update(overrides)
        return {k: v for k, v in data.items() if v is not None}

    def register(self, **overrides):
        return self.client.post("/api/auth/register", data=self.registration_form(**overrides))

    def assert_storage_untouched(self):
        self.bucket_mock.assert_not_called()
        self.client_mock.assert_not_called()


class RegistrationTests(BaseCase):
    def test_registration_without_evidence_creates_student_only_account(self):
        res = self.register()
        self.assertEqual(res.status_code, 201)
        body = res.get_json()
        self.assertTrue(body["ok"])
        self.assertFalse(body["pending"])
        self.assertEqual(body["redirect"], "/dashboard")
        self.assertEqual(body["user"]["roles"], ["student"])

        user = User.query.filter_by(email="maria@example.test").one()
        self.assertEqual(user.roles, ["student"])
        self.assertTrue(user.is_active)
        self.assertFalse(user.is_admin)

    def test_registration_without_roles_field_succeeds(self):
        # No "roles" key is posted at all (the field no longer exists).
        res = self.register()
        self.assertEqual(res.status_code, 201)

    def test_provider_roles_in_payload_do_not_grant_or_record_anything(self):
        res = self.client.post("/api/auth/register", data={
            **self.registration_form(),
            "roles": ["student", "mentor", "coach"],
        })
        self.assertEqual(res.status_code, 201)
        user = User.query.filter_by(email="maria@example.test").one()
        self.assertEqual(user.roles, ["student"])
        self.assertEqual(RoleApplication.query.count(), 0)

    def test_no_credential_or_role_application_rows_are_created(self):
        res = self.register()
        self.assertEqual(res.status_code, 201)
        self.assertEqual(Credential.query.count(), 0)
        self.assertEqual(RoleApplication.query.count(), 0)

    def test_legacy_evidence_fields_are_ignored_and_never_stored(self):
        res = self.client.post(
            "/api/auth/register",
            data={
                **self.registration_form(),
                "roles": ["mentor"],
                "bio": "I have taught mathematics for many years at several schools.",
                "links": ["https://example.test/profile"],
                "proofs": (BytesIO(b"%PDF-1.4 fake"), "diploma.pdf"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(res.status_code, 201)
        user = User.query.filter_by(email="maria@example.test").one()
        self.assertEqual(user.roles, ["student"])
        self.assertEqual(Credential.query.count(), 0)
        self.assertEqual(RoleApplication.query.count(), 0)
        self.assert_storage_untouched()

    def test_registration_never_calls_supabase_storage(self):
        self.register()
        self.assert_storage_untouched()

    def test_registered_user_is_authenticated_immediately(self):
        res = self.register()
        self.assertEqual(res.status_code, 201)
        me = self.client.get("/api/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.get_json()["user"]["email"], "maria@example.test")
        self.assertEqual(me.get_json()["user"]["roles"], ["student"])

    def test_registered_user_can_open_dashboard_without_login_step(self):
        self.register()
        dashboard = self.client.get("/dashboard")
        self.assertEqual(dashboard.status_code, 200)

    def test_existing_validation_is_preserved(self):
        cases = [
            ({"name": "A"}, 400, "name"),
            ({"name": "x" * 81}, 400, "name"),
            ({"email": "not-an-email"}, 400, "email"),
            ({"password": "short1A", "confirm_password": "short1A"}, 400, "password"),
            ({"password": "alllowercase1", "confirm_password": "alllowercase1"}, 400, "password"),
            ({"confirm_password": "Different1Pass"}, 400, "confirmPassword"),
            ({"acknowledged": None}, 400, "ack"),
            ({"acknowledged": "false"}, 400, "ack"),
        ]
        for overrides, status, field in cases:
            with self.subTest(overrides=overrides):
                res = self.register(**overrides)
                self.assertEqual(res.status_code, status)
                self.assertEqual(res.get_json()["field"], field)
        self.assertEqual(User.query.count(), 0)

    def test_duplicate_email_is_rejected_case_insensitively(self):
        self.make_user("maria@example.test", ["student"])
        res = self.register(email="MARIA@example.test")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["field"], "email")
        self.assertEqual(User.query.count(), 1)


class LoginTests(BaseCase):
    def test_active_user_with_no_roles_can_log_in(self):
        user = self.make_user("empty@example.test", [])
        res = self.login(self.client, "empty@example.test")
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["redirect"], "/dashboard")
        db.session.refresh(user)
        self.assertEqual(user.roles, [])  # no silent role assignment

    def test_pending_and_rejected_provider_accounts_can_log_in_and_reach_dashboard(self):
        for status in ("pending", "rejected"):
            with self.subTest(status=status):
                email = f"{status}@example.test"
                user = self.make_user(email, [])
                self.add_application(user, "mentor", status)
                client = self.app.test_client()
                res = self.login(client, email)
                self.assertEqual(res.status_code, 200)
                self.assertEqual(client.get("/dashboard").status_code, 200)
                self.assertEqual(client.get("/api/me").get_json()["user"]["roles"], [])
                db.session.refresh(user)
                self.assertEqual(user.roles, [])

    def test_login_does_not_change_application_status(self):
        user = self.make_user("pending@example.test", [])
        row = self.add_application(user, "coach", "pending")
        self.login(self.client, "pending@example.test")
        db.session.refresh(row)
        self.assertEqual(row.status, "pending")

    def test_pending_and_rejected_users_keep_restricted_access(self):
        for status in ("pending", "rejected"):
            with self.subTest(status=status):
                email = f"restricted-{status}@example.test"
                user = self.make_user(email, [])
                self.add_application(user, "mentor", status)
                client = self.app.test_client()
                self.login(client, email)

                # Provider-only pages and APIs stay closed.
                self.assertEqual(client.get("/schedule").status_code, 403)
                self.assertEqual(client.get("/api/schedule").status_code, 403)
                self.assertEqual(client.get("/api/availability").status_code, 403)
                # Not a student, so cannot request sessions.
                book = client.post("/api/bookings", json={"mentor_id": 1})
                self.assertEqual(book.status_code, 403)
                # Not listed as a provider and no provider role granted.
                self.assertEqual(db.session.get(User, user.id).roles, [])

    def test_inactive_account_cannot_log_in(self):
        self.make_user("inactive@example.test", ["student"], active=False)
        res = self.login(self.client, "inactive@example.test")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(self.client.get("/api/me").status_code, 401)

    def test_inactive_account_with_no_roles_still_cannot_log_in(self):
        self.make_user("inactive-empty@example.test", [], active=False)
        res = self.login(self.client, "inactive-empty@example.test")
        self.assertEqual(res.status_code, 403)

    def test_wrong_password_and_unknown_email_are_rejected(self):
        self.make_user("someone@example.test", [])
        self.assertEqual(self.login(self.client, "someone@example.test", "WrongPass1").status_code, 401)
        self.assertEqual(self.login(self.client, "nobody@example.test").status_code, 401)

    def test_login_response_shape_is_unchanged(self):
        self.make_user("shape@example.test", ["student"])
        body = self.login(self.client, "shape@example.test").get_json()
        self.assertEqual(set(body), {"ok", "message", "redirect", "user"})


class BookingAuthorizationTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.student = self.make_user("student@example.test", ["student"])

    def make_appointment(self, provider, role, status, hours_from_now=24):
        appt = Appointment(
            provider_id=provider.id,
            student_id=self.student.id,
            role=role,
            topic="Test session",
            start_time=naive_utc_now() + timedelta(hours=hours_from_now),
            duration_minutes=60,
            status=status,
        )
        db.session.add(appt)
        db.session.commit()
        return appt

    def act(self, user, appt, action):
        client = self.app.test_client()
        self.assertEqual(self.login(client, user.email).status_code, 200)
        return client.post(f"/api/bookings/{appt.id}/{action}")

    def status_of(self, appt):
        db.session.refresh(appt)
        return appt.status

    # ----- approved providers keep their actions -----
    def test_approved_mentor_can_accept_and_complete(self):
        mentor = self.make_user("mentor@example.test", ["mentor"])
        pending = self.make_appointment(mentor, "mentor", "pending")
        res = self.act(mentor, pending, "accept")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.status_of(pending), "accepted")
        db.session.refresh(pending)
        self.assertTrue(pending.meeting_room)

        finished = self.make_appointment(mentor, "mentor", "accepted", hours_from_now=-48)
        res = self.act(mentor, finished, "complete")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.status_of(finished), "accepted")
        self.assertIsNotNone(finished.provider_confirmed_at)

    def test_approved_coach_can_accept_and_complete(self):
        coach = self.make_user("coach@example.test", ["coach"])
        pending = self.make_appointment(coach, "coach", "pending")
        self.assertEqual(self.act(coach, pending, "accept").status_code, 200)
        self.assertEqual(self.status_of(pending), "accepted")

        finished = self.make_appointment(coach, "coach", "accepted", hours_from_now=-48)
        self.assertEqual(self.act(coach, finished, "complete").status_code, 200)
        self.assertEqual(self.status_of(finished), "accepted")
        self.assertIsNotNone(finished.provider_confirmed_at)

    def test_provider_with_both_approvals_can_act_on_either_role(self):
        both = self.make_user("both@example.test", ["student", "mentor", "coach"])
        for role in ("mentor", "coach"):
            with self.subTest(role=role):
                appt = self.make_appointment(both, role, "pending", hours_from_now=24 if role == "mentor" else 26)
                self.assertEqual(self.act(both, appt, "accept").status_code, 200)

    # ----- unapproved / revoked providers are blocked -----
    def test_unapproved_provider_cannot_accept_or_complete(self):
        for roles in ([], ["student"]):
            with self.subTest(roles=roles):
                provider = self.make_user(f"unapproved{len(roles)}@example.test", roles)
                self.add_application(provider, "mentor", "pending")
                pending = self.make_appointment(provider, "mentor", "pending")
                res = self.act(provider, pending, "accept")
                self.assertEqual(res.status_code, 403)
                self.assertEqual(self.status_of(pending), "pending")
                self.assertFalse(db.session.get(Appointment, pending.id).meeting_room)

                accepted = self.make_appointment(provider, "mentor", "accepted", hours_from_now=-48)
                res = self.act(provider, accepted, "complete")
                self.assertEqual(res.status_code, 403)
                self.assertEqual(self.status_of(accepted), "accepted")

    def test_revoked_provider_cannot_accept_or_complete(self):
        mentor = self.make_user("revoked@example.test", ["student", "mentor"])
        pending = self.make_appointment(mentor, "mentor", "pending")
        accepted = self.make_appointment(mentor, "mentor", "accepted", hours_from_now=-48)

        mentor.roles = ["student"]  # approval removed
        db.session.commit()

        self.assertEqual(self.act(mentor, pending, "accept").status_code, 403)
        self.assertEqual(self.status_of(pending), "pending")
        self.assertEqual(self.act(mentor, accepted, "complete").status_code, 403)
        self.assertEqual(self.status_of(accepted), "accepted")

    def test_provider_deactivated_mid_session_cannot_accept_or_complete(self):
        mentor = self.make_user("deactivated@example.test", ["mentor"])
        pending = self.make_appointment(mentor, "mentor", "pending")
        accepted = self.make_appointment(mentor, "mentor", "accepted", hours_from_now=-48)

        client = self.app.test_client()
        self.assertEqual(self.login(client, mentor.email).status_code, 200)
        mentor.is_active = False
        db.session.commit()

        self.assertNotEqual(client.post(f"/api/bookings/{pending.id}/accept").status_code, 200)
        self.assertNotEqual(client.post(f"/api/bookings/{accepted.id}/complete").status_code, 200)
        self.assertEqual(self.status_of(pending), "pending")
        self.assertEqual(self.status_of(accepted), "accepted")

    def test_helper_rejects_inactive_unknown_role_and_missing_user(self):
        helper = self.routes.provider_approved_for_role
        mentor = self.make_user("helper@example.test", ["mentor"])
        self.assertTrue(helper(mentor, "mentor"))
        self.assertFalse(helper(mentor, "coach"))
        self.assertFalse(helper(mentor, "student"))
        self.assertFalse(helper(mentor, None))
        self.assertFalse(helper(None, "mentor"))
        mentor.is_active = False
        self.assertFalse(helper(mentor, "mentor"))

    # ----- mentor and coach approvals are independent -----
    def test_mentor_approval_does_not_cover_a_coach_appointment(self):
        mentor = self.make_user("only-mentor@example.test", ["mentor"])
        pending = self.make_appointment(mentor, "coach", "pending")
        accepted = self.make_appointment(mentor, "coach", "accepted", hours_from_now=-48)
        self.assertEqual(self.act(mentor, pending, "accept").status_code, 403)
        self.assertEqual(self.act(mentor, accepted, "complete").status_code, 403)
        self.assertEqual(self.status_of(pending), "pending")
        self.assertEqual(self.status_of(accepted), "accepted")

    def test_coach_approval_does_not_cover_a_mentor_appointment(self):
        coach = self.make_user("only-coach@example.test", ["coach"])
        pending = self.make_appointment(coach, "mentor", "pending")
        accepted = self.make_appointment(coach, "mentor", "accepted", hours_from_now=-48)
        self.assertEqual(self.act(coach, pending, "accept").status_code, 403)
        self.assertEqual(self.act(coach, accepted, "complete").status_code, 403)

    def test_losing_one_role_keeps_the_other(self):
        both = self.make_user("split@example.test", ["student", "mentor", "coach"])
        mentor_appt = self.make_appointment(both, "mentor", "pending")
        coach_appt = self.make_appointment(both, "coach", "pending", hours_from_now=26)
        both.roles = ["student", "coach"]  # mentor approval revoked
        db.session.commit()
        self.assertEqual(self.act(both, mentor_appt, "accept").status_code, 403)
        self.assertEqual(self.act(both, coach_appt, "accept").status_code, 200)

    # ----- unchanged behaviour -----
    def test_reject_still_works_for_the_assigned_provider(self):
        provider = self.make_user("reject@example.test", [])
        pending = self.make_appointment(provider, "mentor", "pending")
        res = self.act(provider, pending, "reject")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.status_of(pending), "rejected")

    def test_cancel_still_works_for_student_and_provider(self):
        provider = self.make_user("cancel@example.test", [])
        by_provider = self.make_appointment(provider, "mentor", "accepted")
        self.assertEqual(self.act(provider, by_provider, "cancel").status_code, 200)
        self.assertEqual(self.status_of(by_provider), "cancelled")

        by_student = self.make_appointment(provider, "mentor", "pending")
        self.assertEqual(self.act(self.student, by_student, "cancel").status_code, 200)
        self.assertEqual(self.status_of(by_student), "cancelled")

    def test_other_providers_still_cannot_touch_the_booking(self):
        owner = self.make_user("owner@example.test", ["mentor"])
        other = self.make_user("other@example.test", ["mentor"])
        pending = self.make_appointment(owner, "mentor", "pending")
        self.assertEqual(self.act(other, pending, "accept").status_code, 403)
        self.assertEqual(self.status_of(pending), "pending")

    def test_complete_before_end_time_is_still_refused_for_approved_provider(self):
        mentor = self.make_user("early@example.test", ["mentor"])
        future = self.make_appointment(mentor, "mentor", "accepted", hours_from_now=24)
        res = self.act(mentor, future, "complete")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.status_of(future), "accepted")


if __name__ == "__main__":
    unittest.main()
