"""Batch 2.1C: admin review of Mentor/Coach applications (backend only).

Covers POST /api/admin/applications/<id>/approve | reject | request-revision.

Every database test builds its app through create_app(test_config) with the
verified in-memory SQLite URI. Supabase Storage is never reached: the
credential-storage entry points in the routes module are replaced with mocks
that fail the test if they are called (admin review never needs storage).
No network access, no migration is executed.
"""
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

from flask import g, request_started
from sqlalchemy.exc import IntegrityError

try:
    from Backend import create_app
    from Backend.extensions import db
    from Backend.models import RoleApplication, RoleApplicationHistory, User
except ImportError:
    from __init__ import create_app
    from extensions import db
    from models import RoleApplication, RoleApplicationHistory, User


PASSWORD = "Passw0rdOK"
MENTOR_TEXT = (
    "I have taught high school and college mathematics for eight years "
    "and tutor calculus every week."
)
COACH_TEXT = (
    "Former regional tournament player who now reviews recorded matches "
    "and coaches crosshair placement."
)
NOTE = "Please describe your teaching experience in more detail."
ACTIONS = ("approve", "reject", "request-revision")
STATUS_AFTER = {
    "approve": "approved",
    "reject": "rejected",
    "request-revision": "revision_required",
}
EVENT_AFTER = {
    "approve": "approved",
    "reject": "rejected",
    "request-revision": "revision_requested",
}


def naive_utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class BaseCase(unittest.TestCase):
    CSRF = False

    def setUp(self):
        self.app = create_app({
            "TESTING": True,
            "WTF_CSRF_ENABLED": self.CSRF,
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

        # The outer application context pushed below is reused by every
        # test_client() request, so Flask-Login's g._login_user cache would be
        # shared between clients. Drop it at the start of each request so every
        # request resolves its user from its own client's session cookie.
        request_started.connect(self._reset_login_identity, self.app)
        self.addCleanup(request_started.disconnect, self._reset_login_identity, self.app)

        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.client = self.app.test_client()

    def _reset_login_identity(self, sender, **extra):
        g.pop("_login_user", None)

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    # ----- helpers -----
    def assert_storage_untouched(self):
        self.bucket_mock.assert_not_called()
        self.client_mock.assert_not_called()

    def make_user(self, email, roles, active=True, admin=False):
        user = User(
            display_name=email.split("@")[0],
            email=email,
            is_active=active,
            is_admin=admin,
        )
        user.set_password(PASSWORD)
        user.roles = roles
        db.session.add(user)
        db.session.commit()
        return user

    def login(self, client, email):
        return client.post("/api/auth/login", json={"email": email, "password": PASSWORD})

    def client_for(self, user):
        client = self.app.test_client()
        self.assertEqual(self.login(client, user.email).status_code, 200)
        return client

    def add_application(self, user, role, status="pending", bio=None, links="",
                        review_note=None, reviewed_by=None, reviewed_at=None,
                        submitted_at=None):
        row = RoleApplication(
            user_id=user.id,
            role=role,
            bio=bio or (MENTOR_TEXT if role == "mentor" else COACH_TEXT),
            links=links,
            status=status,
            review_note=review_note,
            reviewed_by=reviewed_by,
            reviewed_at=reviewed_at,
            submitted_at=submitted_at,
        )
        db.session.add(row)
        db.session.commit()
        return row

    def fresh(self, model, pk):
        db.session.expire_all()
        return db.session.get(model, pk)

    def row_state(self, row_id):
        row = self.fresh(RoleApplication, row_id)
        return (row.status, row.review_note, row.reviewed_by, row.reviewed_at,
                row.bio, row.links, row.submitted_at)

    def history(self, application_id=None):
        query = RoleApplicationHistory.query
        if application_id is not None:
            query = query.filter_by(application_id=application_id)
        db.session.expire_all()
        return query.order_by(RoleApplicationHistory.id.asc()).all()

    def review(self, client, application_id, action, payload=None):
        if payload is None:
            return client.post(f"/api/admin/applications/{application_id}/{action}")
        return client.post(f"/api/admin/applications/{application_id}/{action}", json=payload)

    def payload_for(self, action):
        return {"request-revision": {"note": NOTE}, "reject": {"reason": "Not enough detail."}}.get(action)

    def submit(self, client, role, description=MENTOR_TEXT):
        return client.post(
            f"/api/me/applications/{role}",
            data={"experience_description": description},
            content_type="multipart/form-data",
        )


class AdminBase(BaseCase):
    """An applicant with no roles beyond student, and an administrator."""

    def setUp(self):
        super().setUp()
        self.admin = self.make_user("admin@example.test", ["student"], admin=True)
        self.admin_client = self.client_for(self.admin)
        self.user = self.make_user("applicant@example.test", ["student"])


# ---------------------------------------------------------------------------
class ApprovalTests(AdminBase):
    def test_mentor_approval_grants_only_mentor(self):
        row = self.add_application(self.user, "mentor")
        before = naive_utc_now()
        res = self.review(self.admin_client, row.id, "approve")
        after = naive_utc_now()

        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["message"], "Application approved.")
        self.assertEqual(body["application"]["status"], "approved")
        self.assertEqual(body["application"]["id"], row.id)

        row = self.fresh(RoleApplication, row.id)
        self.assertEqual(row.status, "approved")
        self.assertEqual(row.reviewed_by, self.admin.id)
        self.assertTrue(before <= row.reviewed_at <= after)
        self.assertEqual(self.fresh(User, self.user.id).roles, ["mentor", "student"])
        self.assert_storage_untouched()

    def test_coach_approval_grants_only_coach(self):
        row = self.add_application(self.user, "coach")
        res = self.review(self.admin_client, row.id, "approve")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.fresh(RoleApplication, row.id).status, "approved")
        self.assertEqual(self.fresh(User, self.user.id).roles, ["coach", "student"])

    def test_approvals_are_role_specific_and_independent(self):
        mentor_row = self.add_application(self.user, "mentor")
        coach_row = self.add_application(self.user, "coach")

        self.assertEqual(self.review(self.admin_client, mentor_row.id, "approve").status_code, 200)
        user = self.fresh(User, self.user.id)
        self.assertEqual(user.roles, ["mentor", "student"])
        self.assertEqual(self.fresh(RoleApplication, coach_row.id).status, "pending")
        self.assertTrue(self.routes.provider_approved_for_role(user, "mentor"))
        self.assertFalse(self.routes.provider_approved_for_role(user, "coach"))

        self.assertEqual(self.review(self.admin_client, coach_row.id, "approve").status_code, 200)
        user = self.fresh(User, self.user.id)
        self.assertEqual(user.roles, ["coach", "mentor", "student"])
        self.assertTrue(self.routes.provider_approved_for_role(user, "coach"))

    def test_approval_keeps_existing_roles_and_works_for_legacy_empty_roles(self):
        legacy = self.make_user("legacy@example.test", [])
        row = self.add_application(legacy, "mentor")
        self.assertEqual(self.review(self.admin_client, row.id, "approve").status_code, 200)
        self.assertEqual(self.fresh(User, legacy.id).roles, ["mentor"])

        provider = self.make_user("provider@example.test", ["student", "coach"])
        row = self.add_application(provider, "mentor")
        self.assertEqual(self.review(self.admin_client, row.id, "approve").status_code, 200)
        self.assertEqual(self.fresh(User, provider.id).roles, ["coach", "mentor", "student"])

    def test_approval_accepts_a_json_body_and_ignores_it(self):
        row = self.add_application(self.user, "mentor")
        res = self.review(self.admin_client, row.id, "approve", {"reason": "ignored", "note": "ignored"})
        self.assertEqual(res.status_code, 200)
        row = self.fresh(RoleApplication, row.id)
        self.assertEqual(row.status, "approved")
        self.assertIsNone(row.review_note)

    def test_approval_does_not_touch_other_users_or_the_admin(self):
        other = self.make_user("other@example.test", ["student"])
        other_row = self.add_application(other, "mentor")
        row = self.add_application(self.user, "mentor")
        self.review(self.admin_client, row.id, "approve")

        self.assertEqual(self.fresh(User, other.id).roles, ["student"])
        self.assertEqual(self.fresh(RoleApplication, other_row.id).status, "pending")
        self.assertEqual(self.fresh(User, self.admin.id).roles, ["student"])

    def test_approval_with_an_unsupported_role_is_refused(self):
        row = self.add_application(self.user, "student")
        res = self.review(self.admin_client, row.id, "approve")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.fresh(RoleApplication, row.id).status, "pending")
        self.assertEqual(self.fresh(User, self.user.id).roles, ["student"])
        self.assertEqual(self.history(), [])


# ---------------------------------------------------------------------------
class RejectionTests(AdminBase):
    def test_rejection_records_reason_reviewer_and_time_without_granting_a_role(self):
        row = self.add_application(self.user, "mentor")
        before = naive_utc_now()
        res = self.review(self.admin_client, row.id, "reject", {"reason": "  Need more detail  "})
        after = naive_utc_now()

        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(body["message"], "Application rejected.")
        self.assertEqual(body["application"]["status"], "rejected")
        self.assertEqual(body["application"]["review_note"], "Need more detail")

        row = self.fresh(RoleApplication, row.id)
        self.assertEqual(row.status, "rejected")
        self.assertEqual(row.review_note, "Need more detail")
        self.assertEqual(row.reviewed_by, self.admin.id)
        self.assertTrue(before <= row.reviewed_at <= after)
        self.assertEqual(self.fresh(User, self.user.id).roles, ["student"])

    def test_rejection_reason_is_optional(self):
        row = self.add_application(self.user, "coach")
        self.assertEqual(self.review(self.admin_client, row.id, "reject").status_code, 200)
        row = self.fresh(RoleApplication, row.id)
        self.assertEqual(row.status, "rejected")
        self.assertIsNone(row.review_note)

        row2 = self.add_application(self.make_user("b@example.test", ["student"]), "coach")
        self.assertEqual(self.review(self.admin_client, row2.id, "reject", {"reason": "   "}).status_code, 200)
        self.assertIsNone(self.fresh(RoleApplication, row2.id).review_note)

    def test_overlong_rejection_reason_is_truncated_as_before(self):
        row = self.add_application(self.user, "mentor")
        self.review(self.admin_client, row.id, "reject", {"reason": "x" * 800})
        self.assertEqual(len(self.fresh(RoleApplication, row.id).review_note), 500)

    def test_rejection_does_not_remove_an_existing_role_of_the_other_kind(self):
        provider = self.make_user("coach-already@example.test", ["student", "coach"])
        row = self.add_application(provider, "mentor")
        self.review(self.admin_client, row.id, "reject", {"reason": "No."})
        self.assertEqual(self.fresh(User, provider.id).roles, ["student", "coach"])


# ---------------------------------------------------------------------------
class RevisionRequestTests(AdminBase):
    def test_revision_request_succeeds(self):
        row = self.add_application(self.user, "mentor")
        before = naive_utc_now()
        res = self.review(self.admin_client, row.id, "request-revision", {"note": f"  {NOTE}  "})
        after = naive_utc_now()

        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["message"], "Revision requested.")
        self.assertEqual(
            set(body["application"]),
            {"id", "user_id", "role", "status", "review_note", "reviewed_by", "reviewed_at"},
        )
        self.assertEqual(body["application"]["status"], "revision_required")
        self.assertEqual(body["application"]["review_note"], NOTE)
        self.assertEqual(body["application"]["reviewed_by"], self.admin.id)

        row = self.fresh(RoleApplication, row.id)
        self.assertEqual(row.status, "revision_required")
        self.assertEqual(row.review_note, NOTE)
        self.assertEqual(row.reviewed_by, self.admin.id)
        self.assertTrue(before <= row.reviewed_at <= after)
        self.assertEqual(self.fresh(User, self.user.id).roles, ["student"])
        self.assert_storage_untouched()

    def test_reason_is_accepted_as_an_alias_for_note(self):
        row = self.add_application(self.user, "coach")
        res = self.review(self.admin_client, row.id, "request-revision", {"reason": NOTE})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.fresh(RoleApplication, row.id).review_note, NOTE)

    def test_missing_or_blank_note_is_refused_without_any_change(self):
        row = self.add_application(self.user, "mentor")
        original = self.row_state(row.id)
        bodies = [None, {}, {"note": ""}, {"note": "   \n\t "}, {"note": None}, {"reason": ""}]
        for payload in bodies:
            with self.subTest(payload=payload):
                res = self.review(self.admin_client, row.id, "request-revision", payload)
                self.assertEqual(res.status_code, 400)
                body = res.get_json()
                self.assertFalse(body["ok"])
                self.assertEqual(body["field"], "note")
                self.assertEqual(self.row_state(row.id), original)
        self.assertEqual(self.history(), [])

    def test_note_must_be_meaningful_text_within_the_column_limit(self):
        row = self.add_application(self.user, "mentor")
        original = self.row_state(row.id)
        for payload in ({"note": "too short"}, {"note": "x" * 501}, {"note": 12345678901},
                        {"note": ["a long enough note", "in a list"]}, {"note": {"a": "b" * 20}}):
            with self.subTest(payload=payload):
                res = self.review(self.admin_client, row.id, "request-revision", payload)
                self.assertEqual(res.status_code, 400)
                self.assertEqual(res.get_json()["field"], "note")
                self.assertEqual(self.row_state(row.id), original)
        self.assertEqual(self.history(), [])

        exact = "y" * 500
        self.assertEqual(
            self.review(self.admin_client, row.id, "request-revision", {"note": exact}).status_code, 200
        )
        self.assertEqual(self.fresh(RoleApplication, row.id).review_note, exact)

    def test_non_json_body_counts_as_a_missing_note(self):
        row = self.add_application(self.user, "mentor")
        res = self.admin_client.post(
            f"/api/admin/applications/{row.id}/request-revision",
            data="note=" + NOTE,
            content_type="application/x-www-form-urlencoded",
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.fresh(RoleApplication, row.id).status, "pending")

    def test_applicant_sees_the_request_and_can_resubmit_through_the_2_1b_endpoint(self):
        applicant_client = self.client_for(self.user)
        self.assertEqual(self.submit(applicant_client, "mentor").status_code, 201)
        row_id = RoleApplication.query.filter_by(user_id=self.user.id, role="mentor").one().id

        self.assertEqual(
            self.review(self.admin_client, row_id, "request-revision", {"note": NOTE}).status_code, 200
        )
        item = applicant_client.get("/api/me/applications").get_json()["applications"]["mentor"]
        self.assertEqual(item["status"], "revision_required")
        self.assertEqual(item["review_note"], NOTE)
        self.assertTrue(item["can_submit"])

        res = self.submit(applicant_client, "mentor", MENTOR_TEXT + " Now with much more detail.")
        self.assertEqual(res.status_code, 200)
        row = self.fresh(RoleApplication, row_id)
        self.assertEqual(row.status, "pending")
        self.assertIsNone(row.review_note)
        self.assertIsNone(row.reviewed_by)
        self.assertEqual(self.fresh(User, self.user.id).roles, ["student"])

    def test_approved_application_cannot_be_changed_by_a_revision_request(self):
        row = self.add_application(self.user, "mentor")
        self.review(self.admin_client, row.id, "approve")
        approved_state = self.row_state(row.id)
        events = len(self.history())

        res = self.review(self.admin_client, row.id, "request-revision", {"note": NOTE})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.row_state(row.id), approved_state)
        self.assertEqual(self.fresh(User, self.user.id).roles, ["mentor", "student"])
        self.assertEqual(len(self.history()), events)

    def test_revision_request_never_grants_a_role(self):
        for role in ("mentor", "coach"):
            row = self.add_application(self.user, role)
            self.review(self.admin_client, row.id, "request-revision", {"note": NOTE})
        user = self.fresh(User, self.user.id)
        self.assertEqual(user.roles, ["student"])
        self.assertFalse(self.routes.provider_approved_for_role(user, "mentor"))
        self.assertFalse(self.routes.provider_approved_for_role(user, "coach"))


# ---------------------------------------------------------------------------
class AccessTests(AdminBase):
    def test_unauthenticated_requests_are_refused_without_any_change(self):
        row = self.add_application(self.user, "mentor")
        original = self.row_state(row.id)
        anonymous = self.app.test_client()
        for action in ACTIONS:
            with self.subTest(action=action):
                res = self.review(anonymous, row.id, action, {"note": NOTE, "reason": NOTE})
                self.assertIn(res.status_code, (302, 401))
        self.assertEqual(self.row_state(row.id), original)
        self.assertEqual(self.history(), [])
        self.assertEqual(self.fresh(User, self.user.id).roles, ["student"])

    def test_non_admins_are_refused_without_any_change(self):
        row = self.add_application(self.user, "mentor")
        original = self.row_state(row.id)
        approved_mentor = self.make_user("mentor@example.test", ["student", "mentor"])
        non_admins = (self.user, approved_mentor)
        for person in non_admins:
            client = self.client_for(person)
            for action in ACTIONS:
                with self.subTest(person=person.email, action=action):
                    res = self.review(client, row.id, action, {"note": NOTE, "reason": NOTE})
                    self.assertEqual(res.status_code, 403)
                    self.assertFalse(res.get_json()["ok"])
        self.assertEqual(self.row_state(row.id), original)
        self.assertEqual(self.history(), [])
        self.assertEqual(self.fresh(User, self.user.id).roles, ["student"])

    def test_applicant_cannot_approve_their_own_application(self):
        row = self.add_application(self.user, "coach")
        res = self.review(self.client_for(self.user), row.id, "approve")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(self.fresh(RoleApplication, row.id).status, "pending")
        self.assertEqual(self.fresh(User, self.user.id).roles, ["student"])

    def test_admin_access_does_not_require_a_provider_role(self):
        # self.admin only has the student role plus the is_admin flag.
        row = self.add_application(self.user, "mentor")
        self.assertEqual(self.review(self.admin_client, row.id, "request-revision", {"note": NOTE}).status_code, 200)

    def test_deactivated_admin_session_is_refused(self):
        row = self.add_application(self.user, "mentor")
        self.fresh(User, self.admin.id).is_active = False
        db.session.commit()
        res = self.review(self.admin_client, row.id, "approve")
        self.assertIn(res.status_code, (302, 401))
        self.assertEqual(self.fresh(RoleApplication, row.id).status, "pending")


class CsrfTests(BaseCase):
    CSRF = True

    def test_review_post_requires_a_csrf_token(self):
        # Log in with a token (AdminBase's fixture logs in without one).
        self.admin = self.make_user("csrf-admin@example.test", ["student"], admin=True)
        self.user = self.make_user("csrf-applicant@example.test", ["student"])
        client = self.app.test_client()
        token = client.get("/api/csrf-token").get_json()["csrf_token"]
        res = client.post(
            "/api/auth/login",
            json={"email": self.admin.email, "password": PASSWORD},
            headers={"X-CSRFToken": token},
        )
        self.assertEqual(res.status_code, 200)
        row = self.add_application(self.user, "mentor")

        refused = client.post(f"/api/admin/applications/{row.id}/approve")
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(self.fresh(RoleApplication, row.id).status, "pending")
        self.assertEqual(self.fresh(User, self.user.id).roles, ["student"])

        token = client.get("/api/csrf-token").get_json()["csrf_token"]
        allowed = client.post(
            f"/api/admin/applications/{row.id}/approve", headers={"X-CSRFToken": token}
        )
        self.assertEqual(allowed.status_code, 200)


# ---------------------------------------------------------------------------
class RoutingAndStateTests(AdminBase):
    def test_unknown_application_id_is_404_for_every_action(self):
        for action in ACTIONS:
            with self.subTest(action=action):
                res = self.review(self.admin_client, 9999, action, {"note": NOTE})
                self.assertEqual(res.status_code, 404)
                self.assertFalse(res.get_json()["ok"])
        self.assertEqual(self.history(), [])

    def test_non_numeric_application_id_is_404(self):
        res = self.admin_client.post("/api/admin/applications/abc/approve")
        self.assertEqual(res.status_code, 404)

    def test_unknown_action_is_404_and_changes_nothing(self):
        row = self.add_application(self.user, "mentor")
        original = self.row_state(row.id)
        for action in ("delete", "approved", "request_revision", "revise", "Approve"):
            with self.subTest(action=action):
                res = self.review(self.admin_client, row.id, action, {"note": NOTE})
                self.assertEqual(res.status_code, 404)
        self.assertEqual(self.row_state(row.id), original)
        self.assertEqual(self.history(), [])

    def test_review_endpoint_only_accepts_post(self):
        row = self.add_application(self.user, "mentor")
        for action in ACTIONS:
            res = self.admin_client.get(f"/api/admin/applications/{row.id}/{action}")
            self.assertEqual(res.status_code, 405)
        self.assertEqual(self.fresh(RoleApplication, row.id).status, "pending")

    def test_only_pending_applications_can_be_reviewed(self):
        for status in ("approved", "rejected", "revision_required"):
            for action in ACTIONS:
                with self.subTest(status=status, action=action):
                    user = self.make_user(f"{status}-{action}@example.test", ["student"])
                    row = self.add_application(
                        user, "mentor", status, review_note="Earlier decision note.",
                        reviewed_by=77, reviewed_at=naive_utc_now(),
                    )
                    original = self.row_state(row.id)
                    roles_before = self.fresh(User, user.id).roles
                    events = len(self.history())

                    res = self.review(self.admin_client, row.id, action, {"note": NOTE, "reason": NOTE})
                    self.assertEqual(res.status_code, 409)
                    self.assertFalse(res.get_json()["ok"])
                    self.assertEqual(self.row_state(row.id), original)
                    self.assertEqual(self.fresh(User, user.id).roles, roles_before)
                    self.assertEqual(len(self.history()), events)

    def test_duplicate_approval_is_refused_and_keeps_the_first_reviewer(self):
        second_admin = self.make_user("admin2@example.test", ["student"], admin=True)
        second_client = self.client_for(second_admin)
        row = self.add_application(self.user, "mentor")

        self.assertEqual(self.review(self.admin_client, row.id, "approve").status_code, 200)
        first = self.row_state(row.id)
        self.assertEqual(self.review(second_client, row.id, "approve").status_code, 409)
        self.assertEqual(self.row_state(row.id), first)
        self.assertEqual(self.fresh(RoleApplication, row.id).reviewed_by, self.admin.id)
        self.assertEqual(len(self.history(row.id)), 1)
        self.assertEqual(self.fresh(User, self.user.id).roles, ["mentor", "student"])

    def test_reject_then_approve_requires_a_resubmission_first(self):
        row = self.add_application(self.user, "mentor")
        self.assertEqual(self.review(self.admin_client, row.id, "reject", {"reason": "No."}).status_code, 200)
        self.assertEqual(self.review(self.admin_client, row.id, "approve").status_code, 409)
        self.assertEqual(self.fresh(RoleApplication, row.id).status, "rejected")
        self.assertEqual(self.fresh(User, self.user.id).roles, ["student"])


# ---------------------------------------------------------------------------
class HistoryTests(AdminBase):
    def test_each_decision_appends_one_accurate_event(self):
        for action in ACTIONS:
            with self.subTest(action=action):
                user = self.make_user(f"h-{action}@example.test", ["student"])
                submitted = datetime(2026, 5, 4, 10, 30, 15)
                row = self.add_application(
                    user, "coach", links="https://example.test/a\nhttps://example.test/b",
                    submitted_at=submitted,
                )
                self.assertEqual(self.history(row.id), [])
                res = self.review(self.admin_client, row.id, action, self.payload_for(action))
                self.assertEqual(res.status_code, 200)

                events = self.history(row.id)
                self.assertEqual(len(events), 1)
                event = events[0]
                row = self.fresh(RoleApplication, row.id)
                self.assertEqual(event.application_id, row.id)
                self.assertEqual(event.user_id, user.id)
                self.assertEqual(event.role, "coach")
                self.assertEqual(event.event_type, EVENT_AFTER[action])
                self.assertEqual(event.from_status, "pending")
                self.assertEqual(event.to_status, STATUS_AFTER[action])
                # Snapshot of the state the decision replaced.
                self.assertEqual(event.prior_bio, COACH_TEXT)
                self.assertEqual(event.prior_links, "https://example.test/a\nhttps://example.test/b")
                self.assertEqual(event.prior_submitted_at, submitted)
                self.assertIsNone(event.prior_review_note)
                self.assertIsNone(event.prior_reviewed_by)
                self.assertIsNone(event.prior_reviewed_at)
                # The event time is the review time stored on the application.
                self.assertEqual(event.created_at, row.reviewed_at)

    def test_legacy_application_without_submitted_at_falls_back_to_created_at(self):
        row = self.add_application(self.user, "mentor")
        created_at = self.fresh(RoleApplication, row.id).created_at
        self.assertIsNone(self.fresh(RoleApplication, row.id).submitted_at)
        self.review(self.admin_client, row.id, "approve")
        self.assertEqual(self.history(row.id)[0].prior_submitted_at, created_at)

    def test_events_are_per_application_and_per_role(self):
        mentor_row = self.add_application(self.user, "mentor")
        coach_row = self.add_application(self.user, "coach")
        self.review(self.admin_client, mentor_row.id, "approve")
        self.assertEqual(len(self.history(mentor_row.id)), 1)
        self.assertEqual(self.history(coach_row.id), [])

    def test_failed_reviews_never_create_history(self):
        pending = self.add_application(self.user, "mentor")
        done = self.add_application(self.make_user("done@example.test", ["student"]), "mentor", "rejected")
        attempts = [
            (self.admin_client, 9999, "approve", None),                      # not found
            (self.admin_client, pending.id, "request-revision", {}),        # missing note
            (self.admin_client, done.id, "approve", None),                  # not pending
            (self.admin_client, pending.id, "bogus", None),                 # unknown action
            (self.client_for(self.user), pending.id, "approve", None),      # not an admin
            (self.app.test_client(), pending.id, "approve", None),          # anonymous
        ]
        for client, application_id, action, payload in attempts:
            self.review(client, application_id, action, payload)
        self.assertEqual(self.history(), [])

    def test_full_lifecycle_history_through_the_api(self):
        applicant_client = self.client_for(self.user)
        self.assertEqual(self.submit(applicant_client, "mentor", MENTOR_TEXT).status_code, 201)
        row_id = RoleApplication.query.filter_by(user_id=self.user.id, role="mentor").one().id

        note_1 = "Please describe your teaching experience in more detail."
        note_2 = "Please add links to your tutoring profile."
        self.review(self.admin_client, row_id, "request-revision", {"note": note_1})
        decision_1 = self.fresh(RoleApplication, row_id)
        reviewed_at_1 = decision_1.reviewed_at
        self.assertEqual(self.submit(applicant_client, "mentor", MENTOR_TEXT + " Second.").status_code, 200)
        self.review(self.admin_client, row_id, "reject", {"reason": note_2})
        reviewed_at_2 = self.fresh(RoleApplication, row_id).reviewed_at
        self.assertEqual(self.submit(applicant_client, "mentor", MENTOR_TEXT + " Third.").status_code, 200)
        self.assertEqual(self.review(self.admin_client, row_id, "approve").status_code, 200)

        events = self.history(row_id)
        self.assertEqual(
            [(e.event_type, e.from_status, e.to_status) for e in events],
            [
                ("submitted", "not_submitted", "pending"),
                ("revision_requested", "pending", "revision_required"),
                ("resubmitted", "revision_required", "pending"),
                ("rejected", "pending", "rejected"),
                ("resubmitted", "rejected", "pending"),
                ("approved", "pending", "approved"),
            ],
        )
        # Each earlier decision survives the resubmission that replaced it.
        self.assertEqual(events[2].prior_review_note, note_1)
        self.assertEqual(events[2].prior_reviewed_by, self.admin.id)
        self.assertEqual(events[2].prior_reviewed_at, reviewed_at_1)
        self.assertEqual(events[1].created_at, reviewed_at_1)
        self.assertEqual(events[4].prior_review_note, note_2)
        self.assertEqual(events[4].prior_reviewed_by, self.admin.id)
        self.assertEqual(events[4].prior_reviewed_at, reviewed_at_2)
        self.assertEqual(events[3].created_at, reviewed_at_2)
        # Each decision recorded the bio it was made on.
        self.assertEqual(events[1].prior_bio, MENTOR_TEXT)
        self.assertEqual(events[3].prior_bio, MENTOR_TEXT + " Second.")
        self.assertEqual(events[5].prior_bio, MENTOR_TEXT + " Third.")
        self.assertEqual(self.fresh(User, self.user.id).roles, ["mentor", "student"])

    def test_admin_events_are_append_only(self):
        row = self.add_application(self.user, "mentor")
        self.review(self.admin_client, row.id, "approve")
        entry = RoleApplicationHistory.query.one()

        entry.to_status = "tampered"
        with self.assertRaises(ValueError):
            db.session.commit()
        db.session.rollback()
        db.session.delete(db.session.get(RoleApplicationHistory, entry.id))
        with self.assertRaises(ValueError):
            db.session.commit()
        db.session.rollback()
        self.assertEqual(RoleApplicationHistory.query.one().to_status, "approved")


# ---------------------------------------------------------------------------
class AtomicityTests(AdminBase):
    def failing_commit_after_flush(self):
        """Write everything to the open transaction, then fail the commit."""
        def commit():
            db.session.flush()
            raise RuntimeError("db down: secret-connection-detail")
        return commit

    def test_commit_failure_rolls_back_status_role_and_history(self):
        for action in ACTIONS:
            with self.subTest(action=action):
                user = self.make_user(f"atomic-{action}@example.test", ["student"])
                row = self.add_application(user, "mentor")
                original = self.row_state(row.id)

                with mock.patch.object(db.session, "commit", side_effect=self.failing_commit_after_flush()):
                    res = self.review(self.admin_client, row.id, action, self.payload_for(action))

                self.assertEqual(res.status_code, 500)
                body = res.get_json()
                self.assertFalse(body["ok"])
                self.assertNotIn("secret-connection-detail", res.get_data(as_text=True))
                self.assertNotIn("db down", res.get_data(as_text=True))
                self.assertEqual(self.row_state(row.id), original)
                self.assertEqual(self.fresh(User, user.id).roles, ["student"])
                self.assertEqual(self.history(row.id), [])

    def test_failed_review_can_be_retried(self):
        row = self.add_application(self.user, "coach")
        with mock.patch.object(db.session, "commit", side_effect=self.failing_commit_after_flush()):
            self.assertEqual(self.review(self.admin_client, row.id, "approve").status_code, 500)

        self.assertEqual(self.review(self.admin_client, row.id, "approve").status_code, 200)
        self.assertEqual(self.fresh(User, self.user.id).roles, ["coach", "student"])
        self.assertEqual(len(self.history(row.id)), 1)

    def test_database_integrity_error_is_reported_safely(self):
        row = self.add_application(self.user, "mentor")
        error = IntegrityError("UPDATE secret_table", {"p": "secret-value"}, Exception("secret-detail"))
        with mock.patch.object(db.session, "commit", side_effect=error):
            res = self.review(self.admin_client, row.id, "request-revision", {"note": NOTE})
        self.assertEqual(res.status_code, 500)
        text = res.get_data(as_text=True)
        for leaked in ("secret_table", "secret-value", "secret-detail", "IntegrityError"):
            self.assertNotIn(leaked, text)
        self.assertEqual(self.fresh(RoleApplication, row.id).status, "pending")
        self.assertEqual(self.history(), [])

    def test_failure_while_building_the_history_row_changes_nothing(self):
        row = self.add_application(self.user, "mentor")
        original = self.row_state(row.id)
        with mock.patch.object(self.routes, "RoleApplicationHistory", side_effect=RuntimeError("boom")):
            res = self.review(self.admin_client, row.id, "approve")
        self.assertEqual(res.status_code, 500)
        self.assertEqual(self.row_state(row.id), original)
        self.assertEqual(self.fresh(User, self.user.id).roles, ["student"])
        self.assertEqual(self.history(), [])


# ---------------------------------------------------------------------------
class CompatibilityTests(AdminBase):
    def test_existing_reject_and_approve_contract_is_unchanged(self):
        reject_row = self.add_application(self.user, "mentor")
        res = self.review(self.admin_client, reject_row.id, "reject", {"reason": "Need more detail"})
        body = res.get_json()
        self.assertEqual(res.status_code, 200)
        self.assertEqual((body["ok"], body["message"]), (True, "Application rejected."))

        approve_row = self.add_application(self.user, "coach")
        res = self.review(self.admin_client, approve_row.id, "approve")
        body = res.get_json()
        self.assertEqual(res.status_code, 200)
        self.assertEqual((body["ok"], body["message"]), (True, "Application approved."))

        again = self.review(self.admin_client, approve_row.id, "approve")
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.get_json()["message"], "This application was already reviewed.")
        missing = self.review(self.admin_client, 9999, "approve")
        self.assertEqual(missing.get_json()["message"], "Application not found.")

    def test_admin_list_reflects_the_new_status_and_keeps_its_shape(self):
        row = self.add_application(self.user, "mentor")
        pending = self.admin_client.get("/api/admin/applications?status=pending").get_json()
        self.assertEqual([a["id"] for a in pending["applications"]], [row.id])

        self.review(self.admin_client, row.id, "request-revision", {"note": NOTE})
        pending = self.admin_client.get("/api/admin/applications?status=pending").get_json()
        self.assertEqual(pending["applications"], [])
        revised = self.admin_client.get("/api/admin/applications?status=revision_required").get_json()
        self.assertEqual(set(revised), {"ok", "applications"})
        self.assertEqual([a["id"] for a in revised["applications"]], [row.id])
        self.assertEqual(
            set(revised["applications"][0]),
            {"id", "role", "status", "bio", "links", "submitted_at", "user", "credentials"},
        )

    def test_approval_is_visible_to_the_applicant_and_unlocks_only_that_role(self):
        row = self.add_application(self.user, "mentor")
        self.review(self.admin_client, row.id, "approve")
        client = self.client_for(self.user)
        self.assertEqual(client.get("/api/me").get_json()["user"]["roles"], ["mentor", "student"])
        item = client.get("/api/me/applications").get_json()["applications"]
        self.assertEqual(item["mentor"]["status"], "approved")
        self.assertFalse(item["mentor"]["can_submit"])
        self.assertEqual(item["coach"]["status"], "not_submitted")

        # An approved application still cannot be resubmitted by the applicant.
        self.assertEqual(self.submit(client, "mentor").status_code, 409)


if __name__ == "__main__":
    unittest.main()
