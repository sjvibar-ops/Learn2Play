"""Batch 2.1B: post-registration Mentor/Coach applications.

Every database test builds its app through create_app(test_config) with the
verified in-memory SQLite URI. Supabase Storage is never reached: the
credential-storage entry points in the routes module are replaced with mocks.
By default those mocks fail the test if they are called; tests that exercise
file uploads install an in-memory FakeBucket instead. No network access.

The migration tests only read the migration files as text; no migration is
executed and no database other than the in-memory one is opened.
"""
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from unittest import mock

from flask import g, request_started
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import UniqueConstraint

try:
    from Backend import create_app
    from Backend.extensions import db
    from Backend.models import (
        Appointment,
        Credential,
        RoleApplication,
        RoleApplicationHistory,
        User,
    )
except ImportError:
    from __init__ import create_app
    from extensions import db
    from models import (
        Appointment,
        Credential,
        RoleApplication,
        RoleApplicationHistory,
        User,
    )


PASSWORD = "Passw0rdOK"
MENTOR_TEXT = (
    "I have taught high school and college mathematics for eight years "
    "and tutor calculus every week."
)
COACH_TEXT = (
    "Former regional tournament player who now reviews recorded matches "
    "and coaches crosshair placement."
)
VERSIONS_DIR = Path(__file__).resolve().parents[1] / "migrations" / "versions"


def naive_utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def pdf_file(name="certificate.pdf", body=b"%PDF-1.4 test certificate"):
    return (BytesIO(body), name)


def png_file(name="screenshot.png"):
    from PIL import Image
    data = BytesIO()
    Image.new("RGB", (4, 4)).save(data, format="PNG")
    data.seek(0)
    return (data, name)


class FakeBucket:
    """In-memory stand-in for a Supabase Storage bucket."""

    def __init__(self, fail_on=None, remove_raises=False):
        self.fail_on = fail_on          # 1-based upload attempt that fails
        self.remove_raises = remove_raises
        self.attempts = []              # every path an upload was attempted for
        self.stored = {}                # path -> (data, options)
        self.removed = []               # every path passed to remove()

    def upload(self, path, data, options=None):
        self.attempts.append(path)
        if self.fail_on == len(self.attempts):
            raise RuntimeError("simulated storage failure")
        self.stored[path] = (data, options)

    def remove(self, paths):
        self.removed.extend(paths)
        if self.remove_raises:
            raise RuntimeError("simulated cleanup failure")
        for path in paths:
            self.stored.pop(path, None)
        return []


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

        # The outer application context pushed below stays active for the whole
        # test, so every test_client() request reuses it (and its flask.g)
        # instead of creating its own. Flask-Login caches the resolved user on
        # g._login_user, which would let one client's login (or request) decide
        # the identity of another client's next request. Drop that cache at the
        # start of every request so each request resolves its user from its own
        # client's session cookie. Test-only: attached to this test app instance.
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
    def use_bucket(self, bucket):
        """Route credentials_bucket() to an in-memory fake."""
        self.bucket_mock.side_effect = None
        self.bucket_mock.return_value = bucket
        return bucket

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

    def add_application(self, user, role, status, bio=None, links="", review_note=None,
                        reviewed_by=None, reviewed_at=None):
        row = RoleApplication(
            user_id=user.id,
            role=role,
            bio=bio or ("Original application text. " * 3),
            links=links,
            status=status,
            review_note=review_note,
            reviewed_by=reviewed_by,
            reviewed_at=reviewed_at,
        )
        db.session.add(row)
        db.session.commit()
        return row

    def get_row(self, user, role):
        return RoleApplication.query.filter_by(user_id=user.id, role=role).first()

    def history_for(self, user, role):
        return (
            RoleApplicationHistory.query.filter_by(user_id=user.id, role=role)
            .order_by(RoleApplicationHistory.id.asc())
            .all()
        )

    def post_application(self, client, role, description=MENTOR_TEXT, links=None,
                         files=None, extra=None, headers=None):
        data = {}
        if description is not None:
            data["experience_description"] = description
        if links is not None:
            data["links"] = links
        if files:
            data["proofs"] = files
        if extra:
            data.update(extra)
        return client.post(
            f"/api/me/applications/{role}",
            data=data,
            content_type="multipart/form-data",
            headers=headers,
        )

    def counts(self):
        return (
            RoleApplication.query.count(),
            Credential.query.count(),
            RoleApplicationHistory.query.count(),
        )


# ---------------------------------------------------------------------------
class ApplicationStatusTests(BaseCase):
    """GET /api/me/applications."""

    def test_endpoints_require_login(self):
        get = self.client.get("/api/me/applications")
        post = self.client.post(
            "/api/me/applications/mentor", data={"experience_description": MENTOR_TEXT}
        )
        self.assertIn(get.status_code, (302, 401))
        self.assertIn(post.status_code, (302, 401))
        self.assertEqual(self.counts(), (0, 0, 0))
        self.assert_storage_untouched()

    def test_new_student_has_not_submitted_for_both_roles(self):
        user = self.make_user("new@example.test", ["student"])
        res = self.client_for(user).get("/api/me/applications")
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(set(body["applications"]), {"mentor", "coach"})
        for role in ("mentor", "coach"):
            with self.subTest(role=role):
                item = body["applications"][role]
                self.assertEqual(item["role"], role)
                self.assertEqual(item["status"], "not_submitted")
                self.assertTrue(item["can_submit"])
                self.assertEqual(item["experience_description"], "")
                self.assertEqual(item["links"], [])
                self.assertEqual(item["credentials"], [])
                self.assertIsNone(item["review_note"])
                self.assertIsNone(item["submitted_at"])
                self.assertIsNone(item["reviewed_at"])
        self.assertEqual(body["legacy_credentials"], [])
        self.assert_storage_untouched()

    def test_roles_are_reported_independently(self):
        user = self.make_user("mixed@example.test", ["student"])
        self.add_application(user, "mentor", "pending")
        self.add_application(
            user, "coach", "rejected",
            review_note="Please add more detail.",
            reviewed_at=naive_utc_now(),
        )
        apps = self.client_for(user).get("/api/me/applications").get_json()["applications"]

        self.assertEqual(apps["mentor"]["status"], "pending")
        self.assertFalse(apps["mentor"]["can_submit"])
        self.assertIsNone(apps["mentor"]["review_note"])

        self.assertEqual(apps["coach"]["status"], "rejected")
        self.assertTrue(apps["coach"]["can_submit"])
        self.assertEqual(apps["coach"]["review_note"], "Please add more detail.")
        self.assertIsNotNone(apps["coach"]["reviewed_at"])
        self.assertIsNotNone(apps["coach"]["submitted_at"])

    def test_revision_required_is_reported_with_note_and_allows_resubmission(self):
        user = self.make_user("revise@example.test", ["student"])
        self.add_application(user, "mentor", "revision_required", review_note="Add a link.")
        item = self.client_for(user).get("/api/me/applications").get_json()["applications"]["mentor"]
        self.assertEqual(item["status"], "revision_required")
        self.assertEqual(item["review_note"], "Add a link.")
        self.assertTrue(item["can_submit"])

    def test_existing_approved_role_without_application_row_reads_as_approved(self):
        user = self.make_user("old-approved@example.test", ["student", "mentor"])
        apps = self.client_for(user).get("/api/me/applications").get_json()["applications"]
        self.assertEqual(apps["mentor"]["status"], "approved")
        self.assertFalse(apps["mentor"]["can_submit"])
        self.assertEqual(apps["coach"]["status"], "not_submitted")
        self.assertTrue(apps["coach"]["can_submit"])

    def test_legacy_provider_only_account_with_rejected_application(self):
        user = self.make_user("legacy@example.test", [])
        self.add_application(user, "mentor", "rejected", review_note="Not enough detail.")
        item = self.client_for(user).get("/api/me/applications").get_json()["applications"]["mentor"]
        self.assertEqual(item["status"], "rejected")
        self.assertTrue(item["can_submit"])

    def test_legacy_row_without_submitted_at_falls_back_to_created_at(self):
        user = self.make_user("fallback@example.test", ["student"])
        row = self.add_application(user, "mentor", "pending")
        self.assertIsNone(row.submitted_at)
        item = self.client_for(user).get("/api/me/applications").get_json()["applications"]["mentor"]
        self.assertEqual(item["submitted_at"], row.created_at.isoformat())

    def test_credentials_show_safe_metadata_only(self):
        user = self.make_user("safe@example.test", ["student"])
        client = self.client_for(user)
        self.use_bucket(FakeBucket())
        self.assertEqual(
            self.post_application(client, "mentor", files=[pdf_file("diploma.pdf")]).status_code, 201
        )
        stored_name = Credential.query.one().stored_name

        res = client.get("/api/me/applications")
        creds = res.get_json()["applications"]["mentor"]["credentials"]
        self.assertEqual(len(creds), 1)
        self.assertEqual(set(creds[0]), {"id", "name", "uploaded_at"})
        self.assertEqual(creds[0]["name"], "diploma.pdf")
        text = res.get_data(as_text=True)
        self.assertNotIn(stored_name, text)
        self.assertNotIn("stored_name", text)
        self.assertNotIn("/admin/credentials", text)

    def test_legacy_credentials_are_listed_separately_and_not_attributed(self):
        user = self.make_user("legacy-files@example.test", ["student"])
        self.add_application(user, "mentor", "pending")
        self.add_application(user, "coach", "pending")
        db.session.add(Credential(
            user_id=user.id, original_name="old.pdf", stored_name="legacy/old.pdf"
        ))
        db.session.commit()

        body = self.client_for(user).get("/api/me/applications").get_json()
        self.assertEqual([c["name"] for c in body["legacy_credentials"]], ["old.pdf"])
        self.assertEqual(body["applications"]["mentor"]["credentials"], [])
        self.assertEqual(body["applications"]["coach"]["credentials"], [])
        # Rows with an unknown application are left exactly as they were.
        self.assertIsNone(Credential.query.one().application_id)

    def test_limits_are_exposed(self):
        user = self.make_user("limits@example.test", ["student"])
        limits = self.client_for(user).get("/api/me/applications").get_json()["limits"]
        self.assertEqual(limits["experience_min_length"], 50)
        self.assertEqual(limits["experience_max_length"], 1500)
        self.assertEqual(limits["max_files"], 5)
        self.assertEqual(limits["max_links"], 5)

    def test_status_response_is_not_cacheable(self):
        user = self.make_user("cache@example.test", ["student"])
        res = self.client_for(user).get("/api/me/applications")
        self.assertIn("no-store", res.headers.get("Cache-Control", ""))


# ---------------------------------------------------------------------------
class SubmitWithoutFilesTests(BaseCase):
    """Zero-file submissions: descriptions are mandatory, evidence is optional."""

    def test_student_applies_as_mentor_after_registration(self):
        # Full path: register (2.1A) and then apply, in the same session.
        client = self.app.test_client()
        res = client.post("/api/auth/register", data={
            "name": "Maria Santos",
            "email": "maria@example.test",
            "password": PASSWORD,
            "confirm_password": PASSWORD,
            "acknowledged": "true",
        })
        self.assertEqual(res.status_code, 201)
        self.assertEqual(RoleApplication.query.count(), 0)

        res = self.post_application(client, "mentor")
        self.assertEqual(res.status_code, 201)
        body = res.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["application"]["role"], "mentor")
        self.assertEqual(body["application"]["status"], "pending")
        self.assertEqual(body["application"]["experience_description"], MENTOR_TEXT)
        self.assertEqual(body["application"]["credentials"], [])
        self.assertFalse(body["application"]["can_submit"])

        user = User.query.filter_by(email="maria@example.test").one()
        self.assertEqual(user.roles, ["student"])
        row = self.get_row(user, "mentor")
        self.assertEqual(row.status, "pending")
        self.assertIsNotNone(row.submitted_at)
        self.assertEqual(Credential.query.count(), 0)
        self.assert_storage_untouched()

    def test_student_applies_as_coach_after_registration(self):
        user = self.make_user("coach-applicant@example.test", ["student"])
        res = self.post_application(self.client_for(user), "coach", description=COACH_TEXT)
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.get_json()["application"]["role"], "coach")
        self.assertEqual(self.get_row(user, "coach").status, "pending")
        self.assertIsNone(self.get_row(user, "mentor"))
        self.assertEqual(db.session.get(User, user.id).roles, ["student"])
        self.assert_storage_untouched()

    def test_student_applies_for_both_roles_independently(self):
        user = self.make_user("both@example.test", ["student"])
        client = self.client_for(user)

        self.assertEqual(self.post_application(client, "mentor").status_code, 201)
        # The mentor application being pending does not affect the coach one.
        coach_state = client.get("/api/me/applications").get_json()["applications"]["coach"]
        self.assertEqual(coach_state["status"], "not_submitted")
        self.assertTrue(coach_state["can_submit"])

        self.assertEqual(
            self.post_application(client, "coach", description=COACH_TEXT).status_code, 201
        )
        mentor = self.get_row(user, "mentor")
        coach = self.get_row(user, "coach")
        self.assertNotEqual(mentor.id, coach.id)
        self.assertEqual(mentor.bio, MENTOR_TEXT)
        self.assertEqual(coach.bio, COACH_TEXT)
        self.assertEqual(db.session.get(User, user.id).roles, ["student"])
        self.assertEqual(Credential.query.count(), 0)
        self.assert_storage_untouched()

    def test_json_submission_without_files_succeeds(self):
        user = self.make_user("json@example.test", ["student"])
        res = self.client_for(user).post(
            "/api/me/applications/mentor",
            json={"experience_description": MENTOR_TEXT, "links": ["https://example.test/a"]},
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(self.get_row(user, "mentor").links, "https://example.test/a")
        self.assert_storage_untouched()

    def test_legacy_account_without_student_role_can_apply(self):
        user = self.make_user("no-student@example.test", [])
        res = self.post_application(self.client_for(user), "mentor")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(db.session.get(User, user.id).roles, [])

    def test_response_does_not_claim_approval(self):
        user = self.make_user("claim@example.test", ["student"])
        body = self.post_application(self.client_for(user), "mentor").get_json()
        self.assertIn("submitted", body["message"].lower())
        self.assertNotIn("approved", body["message"].lower())

    def test_links_are_saved_from_repeated_and_newline_separated_values(self):
        user = self.make_user("links@example.test", ["student"])
        res = self.post_application(
            self.client_for(user), "coach", description=COACH_TEXT,
            links=["https://example.test/a", "https://example.test/b",
                   "https://example.test/c\nhttps://example.test/d", ""],
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(
            res.get_json()["application"]["links"],
            ["https://example.test/a", "https://example.test/b",
             "https://example.test/c", "https://example.test/d"],
        )

    def test_description_is_stripped_and_boundary_lengths_are_accepted(self):
        for length in (50, 1500):
            with self.subTest(length=length):
                user = self.make_user(f"len{length}@example.test", ["student"])
                res = self.post_application(
                    self.client_for(user), "mentor", description=f"  {'x' * length}  "
                )
                self.assertEqual(res.status_code, 201)
                self.assertEqual(self.get_row(user, "mentor").bio, "x" * length)

    def test_wrong_method_is_rejected(self):
        user = self.make_user("method@example.test", ["student"])
        res = self.client_for(user).get("/api/me/applications/mentor")
        self.assertEqual(res.status_code, 405)


# ---------------------------------------------------------------------------
class SubmitWithFilesTests(BaseCase):
    """Optional evidence files are attached to the correct application only."""

    def setUp(self):
        super().setUp()
        self.bucket = self.use_bucket(FakeBucket())

    def test_files_are_scoped_to_their_own_application(self):
        user = self.make_user("scoped@example.test", ["student"])
        client = self.client_for(user)
        r1 = self.post_application(
            client, "mentor", files=[pdf_file("m1.pdf"), png_file("m2.png")]
        )
        r2 = self.post_application(
            client, "coach", description=COACH_TEXT, files=[pdf_file("c1.pdf")]
        )
        self.assertEqual((r1.status_code, r2.status_code), (201, 201))

        mentor = self.get_row(user, "mentor")
        coach = self.get_row(user, "coach")

        def names(row):
            return sorted(
                c.original_name for c in Credential.query.filter_by(application_id=row.id)
            )

        self.assertEqual(names(mentor), ["m1.pdf", "m2.png"])
        self.assertEqual(names(coach), ["c1.pdf"])
        self.assertEqual(len(self.bucket.stored), 3)

        apps = client.get("/api/me/applications").get_json()["applications"]
        self.assertEqual(sorted(c["name"] for c in apps["mentor"]["credentials"]), ["m1.pdf", "m2.png"])
        self.assertEqual([c["name"] for c in apps["coach"]["credentials"]], ["c1.pdf"])
        self.assertEqual(db.session.get(User, user.id).roles, ["student"])

    def test_storage_paths_are_generated_by_the_server(self):
        user = self.make_user("paths@example.test", ["student"])
        res = self.post_application(
            self.client_for(user), "mentor", files=[pdf_file("diploma.pdf")],
            extra={
                "stored_name": "../../evil.pdf",
                "application_id": "999",
                "user_id": "999",
                "id": "999",
            },
        )
        self.assertEqual(res.status_code, 201)
        cred = Credential.query.one()
        row = self.get_row(user, "mentor")
        self.assertTrue(cred.stored_name.startswith(f"{user.id}/mentor/"))
        self.assertTrue(cred.stored_name.endswith(".pdf"))
        self.assertLessEqual(len(cred.stored_name), 64)
        self.assertNotIn("evil", cred.stored_name)
        self.assertEqual(self.bucket.attempts, [cred.stored_name])
        self.assertEqual(cred.user_id, user.id)
        self.assertEqual(cred.application_id, row.id)
        self.assertNotEqual(row.id, 999)

    def test_uploaded_content_and_type_are_passed_to_storage(self):
        user = self.make_user("content@example.test", ["student"])
        body = b"%PDF-1.4 exact bytes"
        self.post_application(
            self.client_for(user), "mentor", files=[pdf_file("a.pdf", body), png_file("b.png")]
        )
        by_name = {c.original_name: c for c in Credential.query.all()}
        data, options = self.bucket.stored[by_name["a.pdf"].stored_name]
        self.assertEqual(data, body)
        self.assertEqual(options, {"content-type": "application/pdf", "upsert": "false"})
        self.assertEqual(
            self.bucket.stored[by_name["b.png"].stored_name][1], {"content-type": "image/png", "upsert": "false"}
        )

    def test_invalid_file_stops_everything_before_any_upload(self):
        user = self.make_user("badfile@example.test", ["student"])
        res = self.post_application(
            self.client_for(user), "mentor",
            files=[pdf_file("good.pdf"), (BytesIO(b"plain text"), "notes.txt")],
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["field"], "proofs")
        self.assertEqual(self.bucket.attempts, [])
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_file_that_does_not_match_its_extension_is_rejected(self):
        user = self.make_user("fake-pdf@example.test", ["student"])
        res = self.post_application(
            self.client_for(user), "mentor", files=[(BytesIO(b"not really a pdf"), "fake.pdf")]
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.bucket.attempts, [])
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_file_over_five_megabytes_is_rejected(self):
        user = self.make_user("big@example.test", ["student"])
        too_big = b"%PDF-" + b"0" * (5 * 1024 * 1024)
        res = self.post_application(
            self.client_for(user), "mentor", files=[(BytesIO(too_big), "big.pdf")]
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["field"], "proofs")
        self.assertEqual(self.bucket.attempts, [])

    def test_more_than_five_files_in_one_request_is_rejected(self):
        user = self.make_user("many@example.test", ["student"])
        res = self.post_application(
            self.client_for(user), "mentor", files=[pdf_file(f"f{i}.pdf") for i in range(6)]
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["field"], "proofs")
        self.assertEqual(self.bucket.attempts, [])
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_file_limit_is_cumulative_across_resubmissions(self):
        user = self.make_user("cumulative@example.test", ["student"])
        client = self.client_for(user)
        self.assertEqual(
            self.post_application(
                client, "mentor", files=[pdf_file(f"a{i}.pdf") for i in range(3)]
            ).status_code,
            201,
        )
        row = self.get_row(user, "mentor")
        row.status = "rejected"
        row.review_note = "Need more."
        db.session.commit()

        res = self.post_application(
            client, "mentor", files=[pdf_file(f"b{i}.pdf") for i in range(3)]
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["field"], "proofs")
        self.assertEqual(Credential.query.filter_by(application_id=row.id).count(), 3)
        self.assertEqual(self.get_row(user, "mentor").status, "rejected")
        self.assertEqual(len(self.bucket.attempts), 3)

        res = self.post_application(
            client, "mentor", files=[pdf_file(f"c{i}.pdf") for i in range(2)]
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(Credential.query.filter_by(application_id=row.id).count(), 5)

    def test_resubmission_keeps_existing_files_and_adds_new_ones(self):
        user = self.make_user("keep-files@example.test", ["student"])
        client = self.client_for(user)
        self.post_application(client, "coach", description=COACH_TEXT, files=[pdf_file("first.pdf")])
        row = self.get_row(user, "coach")
        row.status = "rejected"
        db.session.commit()

        res = self.post_application(
            client, "coach", description=COACH_TEXT + " Updated.", files=[png_file("second.png")]
        )
        self.assertEqual(res.status_code, 200)
        names = [c["name"] for c in res.get_json()["application"]["credentials"]]
        self.assertEqual(names, ["first.pdf", "second.png"])

    def test_admin_list_shows_each_application_only_its_own_files(self):
        user = self.make_user("admin-view@example.test", ["student"])
        admin = self.make_user("admin@example.test", ["student"], admin=True)
        client = self.client_for(user)
        self.post_application(client, "mentor", files=[pdf_file("mentor-only.pdf")])
        self.post_application(client, "coach", description=COACH_TEXT, files=[png_file("coach-only.png")])

        res = self.client_for(admin).get("/api/admin/applications?status=pending")
        self.assertEqual(res.status_code, 200)
        by_role = {a["role"]: a for a in res.get_json()["applications"]}
        self.assertEqual([c["name"] for c in by_role["mentor"]["credentials"]], ["mentor-only.pdf"])
        self.assertEqual([c["name"] for c in by_role["coach"]["credentials"]], ["coach-only.png"])


# ---------------------------------------------------------------------------
class StateMachineTests(BaseCase):
    """Duplicate, approved, rejected and revision_required handling."""

    def test_duplicate_pending_submission_is_rejected(self):
        user = self.make_user("dup@example.test", ["student"])
        client = self.client_for(user)
        bucket = self.use_bucket(FakeBucket())
        self.assertEqual(self.post_application(client, "mentor").status_code, 201)
        before = self.counts()

        res = self.post_application(
            client, "mentor", description=COACH_TEXT, files=[pdf_file()]
        )
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["field"], "status")
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.get_row(user, "mentor").bio, MENTOR_TEXT)
        self.assertEqual(bucket.attempts, [])

    def test_approved_application_cannot_be_overwritten(self):
        user = self.make_user("approved@example.test", ["student", "mentor"])
        row = self.add_application(user, "mentor", "approved", bio="Approved text. " * 5)
        original = row.bio
        bucket = self.use_bucket(FakeBucket())

        res = self.post_application(
            self.client_for(user), "mentor", description=COACH_TEXT, files=[pdf_file()]
        )
        self.assertEqual(res.status_code, 409)
        row = self.get_row(user, "mentor")
        self.assertEqual(row.status, "approved")
        self.assertEqual(row.bio, original)
        self.assertEqual(self.counts(), (1, 0, 0))
        self.assertEqual(bucket.attempts, [])
        self.assertEqual(db.session.get(User, user.id).roles, ["student", "mentor"])

    def test_approved_row_with_removed_role_still_cannot_be_overwritten(self):
        user = self.make_user("revoked@example.test", ["student"])
        row = self.add_application(user, "coach", "approved", bio="Approved text. " * 5)
        original = row.bio
        res = self.post_application(self.client_for(user), "coach", description=COACH_TEXT)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.get_row(user, "coach").bio, original)
        self.assertEqual(self.history_for(user, "coach"), [])

    def test_user_who_already_holds_a_role_cannot_create_an_application_for_it(self):
        user = self.make_user("holder@example.test", ["student", "coach"])
        res = self.post_application(self.client_for(user), "coach", description=COACH_TEXT)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_application_with_unrecognised_status_is_not_overwritten(self):
        user = self.make_user("odd@example.test", ["student"])
        row = self.add_application(user, "mentor", "weird")
        original = row.bio
        res = self.post_application(self.client_for(user), "mentor")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.get_row(user, "mentor").bio, original)
        self.assertEqual(self.get_row(user, "mentor").status, "weird")

    def test_rejected_application_can_be_corrected_and_resubmitted(self):
        user = self.make_user("rejected@example.test", ["student"])
        reviewed_at = naive_utc_now() - timedelta(days=1)
        row = self.add_application(
            user, "mentor", "rejected", bio="Original description. " * 3,
            links="https://example.test/old", review_note="Add details",
            reviewed_by=99, reviewed_at=reviewed_at,
        )
        original_bio, original_links = row.bio, row.links
        original_created = row.created_at
        client = self.client_for(user)

        res = self.post_application(
            client, "mentor", description=MENTOR_TEXT, links=["https://example.test/new"]
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["application"]["status"], "pending")
        self.assertIn("resubmitted", res.get_json()["message"])

        row = self.get_row(user, "mentor")
        self.assertEqual(row.status, "pending")
        self.assertEqual(row.bio, MENTOR_TEXT)
        self.assertEqual(row.links, "https://example.test/new")
        self.assertIsNone(row.review_note)
        self.assertIsNone(row.reviewed_by)
        self.assertIsNone(row.reviewed_at)
        self.assertIsNotNone(row.submitted_at)
        self.assertEqual(row.created_at, original_created)
        self.assertEqual(RoleApplication.query.count(), 1)

        history = self.history_for(user, "mentor")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].event_type, "resubmitted")
        self.assertEqual(history[0].from_status, "rejected")
        self.assertEqual(history[0].to_status, "pending")
        self.assertEqual(history[0].prior_bio, original_bio)
        self.assertEqual(history[0].prior_links, original_links)
        self.assertEqual(history[0].prior_review_note, "Add details")
        self.assertEqual(history[0].prior_reviewed_by, 99)
        self.assertEqual(history[0].prior_reviewed_at, reviewed_at)

        # Resubmission never grants a role.
        self.assertEqual(db.session.get(User, user.id).roles, ["student"])

    def test_revision_required_application_can_be_resubmitted(self):
        user = self.make_user("revision@example.test", ["student"])
        self.add_application(
            user, "coach", "revision_required", review_note="Link your tournament profile."
        )
        res = self.post_application(
            self.client_for(user), "coach", description=COACH_TEXT,
            links=["https://example.test/profile"],
        )
        self.assertEqual(res.status_code, 200)
        row = self.get_row(user, "coach")
        self.assertEqual(row.status, "pending")
        self.assertEqual(row.bio, COACH_TEXT)
        self.assertIsNone(row.review_note)

        history = self.history_for(user, "coach")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].from_status, "revision_required")
        self.assertEqual(history[0].prior_review_note, "Link your tournament profile.")
        self.assertEqual(db.session.get(User, user.id).roles, ["student"])

    def test_rejection_and_resubmission_can_repeat_without_a_cooldown_or_limit(self):
        user = self.make_user("repeat@example.test", ["student"])
        client = self.client_for(user)
        self.assertEqual(self.post_application(client, "mentor").status_code, 201)

        for i in range(4):
            row = self.get_row(user, "mentor")
            row.status = "rejected"
            row.review_note = f"round {i}"
            row.reviewed_at = naive_utc_now()
            db.session.commit()
            res = self.post_application(
                client, "mentor", description=f"{MENTOR_TEXT} Revision {i}"
            )
            self.assertEqual(res.status_code, 200, f"resubmission {i} was refused")

        self.assertEqual(self.get_row(user, "mentor").status, "pending")
        self.assertEqual(len(self.history_for(user, "mentor")), 5)

    def test_existing_admin_review_flow_still_works_on_resubmitted_applications(self):
        user = self.make_user("flow@example.test", ["student"])
        admin = self.make_user("flow-admin@example.test", ["student"], admin=True)
        user_client = self.client_for(user)
        admin_client = self.client_for(admin)

        self.assertEqual(self.post_application(user_client, "mentor").status_code, 201)
        row_id = self.get_row(user, "mentor").id
        res = admin_client.post(
            f"/api/admin/applications/{row_id}/reject", json={"reason": "Need more detail"}
        )
        self.assertEqual(res.status_code, 200)

        item = user_client.get("/api/me/applications").get_json()["applications"]["mentor"]
        self.assertEqual(item["status"], "rejected")
        self.assertEqual(item["review_note"], "Need more detail")
        self.assertTrue(item["can_submit"])

        self.assertEqual(
            self.post_application(user_client, "mentor", description=COACH_TEXT).status_code, 200
        )
        res = admin_client.post(f"/api/admin/applications/{row_id}/approve")
        self.assertEqual(res.status_code, 200)

        db.session.refresh(user)
        self.assertEqual(user.roles, ["mentor", "student"])
        # An approved application cannot be changed through the user endpoint.
        res = self.post_application(user_client, "mentor")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.get_row(user, "mentor").status, "approved")
        # The prior rejection stays in history. Since Batch 2.1C the admin
        # decisions are logged too, so the last row is the approval event;
        # look at the resubmission that replaced the rejection.
        history = self.history_for(user, "mentor")
        resubmitted = [h for h in history if h.event_type == "resubmitted"]
        self.assertEqual(resubmitted[-1].prior_review_note, "Need more detail")


# ---------------------------------------------------------------------------
class ValidationTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user("validate@example.test", ["student"])
        self.client_a = self.client_for(self.user)

    def assert_rejected(self, res, field, status=400):
        self.assertEqual(res.status_code, status)
        body = res.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["field"], field)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_unknown_roles_are_rejected(self):
        for role in ("student", "admin", "teacher", "MENTOR", "coach1", "0"):
            with self.subTest(role=role):
                res = self.post_application(self.client_a, role)
                self.assert_rejected(res, "role", 404)
        self.assertEqual(db.session.get(User, self.user.id).roles, ["student"])
        self.assert_storage_untouched()

    def test_invalid_experience_descriptions_are_rejected(self):
        cases = {
            "missing": None,
            "empty": "",
            "blank": " " * 80,
            "too short": "x" * 49,
            "too long": "x" * 1501,
        }
        for label, text in cases.items():
            with self.subTest(case=label):
                res = self.post_application(self.client_a, "mentor", description=text)
                self.assert_rejected(res, "experience_description")
        self.assert_storage_untouched()

    def test_non_string_json_descriptions_are_rejected(self):
        for value in (123, None, ["a" * 60], {"text": "a" * 60}, True):
            with self.subTest(value=value):
                res = self.client_a.post(
                    "/api/me/applications/mentor", json={"experience_description": value}
                )
                self.assert_rejected(res, "experience_description")

    def test_malformed_json_bodies_are_rejected(self):
        for raw in ("[1, 2]", "{bad json", '"just a string"', "null"):
            with self.subTest(body=raw):
                res = self.client_a.post(
                    "/api/me/applications/mentor", data=raw, content_type="application/json"
                )
                self.assert_rejected(res, None)

    def test_nul_characters_are_rejected(self):
        res = self.client_a.post(
            "/api/me/applications/mentor", json={"experience_description": MENTOR_TEXT + "\x00"}
        )
        self.assert_rejected(res, "experience_description")

    def test_invalid_links_are_rejected(self):
        cases = {
            "ftp scheme": ["ftp://example.test/x"],
            "not a url": ["not a link"],
            "javascript scheme": ["javascript:alert(1)"],
            "six links": [f"https://example.test/{i}" for i in range(6)],
            "too long": ["https://example.test/" + "a" * 300],
        }
        for label, links in cases.items():
            with self.subTest(case=label):
                res = self.post_application(self.client_a, "mentor", links=links)
                self.assert_rejected(res, "links")

    def test_invalid_json_link_types_are_rejected(self):
        for value in (123, [1, 2], {"a": "https://example.test"}):
            with self.subTest(value=value):
                res = self.client_a.post(
                    "/api/me/applications/mentor",
                    json={"experience_description": MENTOR_TEXT, "links": value},
                )
                self.assert_rejected(res, "links")

    def test_validation_failures_never_touch_storage(self):
        self.post_application(self.client_a, "mentor", description="short", files=[pdf_file()])
        self.post_application(self.client_a, "mentor", links=["nope"], files=[pdf_file()])
        self.assertEqual(self.counts(), (0, 0, 0))
        self.assert_storage_untouched()


# ---------------------------------------------------------------------------
class OwnershipTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.bucket = self.use_bucket(FakeBucket())
        self.alice = self.make_user("alice@example.test", ["student"])
        self.bob = self.make_user("bob@example.test", ["student"])
        self.alice_client = self.client_for(self.alice)
        self.bob_client = self.client_for(self.bob)

    def test_other_user_cannot_alter_application_evidence(self):
        self.assertEqual(
            self.post_application(
                self.alice_client, "mentor", files=[pdf_file("alice.pdf")]
            ).status_code,
            201,
        )
        alice_row = self.get_row(self.alice, "mentor")
        alice_cred = Credential.query.one()
        alice_bio = alice_row.bio

        res = self.post_application(
            self.bob_client, "mentor", description=COACH_TEXT, files=[pdf_file("bob.pdf")],
            extra={
                "application_id": str(alice_row.id),
                "user_id": str(self.alice.id),
                "id": str(alice_row.id),
            },
        )
        self.assertEqual(res.status_code, 201)

        bob_row = self.get_row(self.bob, "mentor")
        self.assertNotEqual(bob_row.id, alice_row.id)
        self.assertEqual(bob_row.user_id, self.bob.id)

        alice_creds = Credential.query.filter_by(application_id=alice_row.id).all()
        self.assertEqual([c.id for c in alice_creds], [alice_cred.id])
        bob_creds = Credential.query.filter_by(application_id=bob_row.id).all()
        self.assertEqual([(c.original_name, c.user_id) for c in bob_creds], [("bob.pdf", self.bob.id)])

        db.session.refresh(alice_row)
        self.assertEqual(alice_row.bio, alice_bio)
        self.assertEqual(alice_row.status, "pending")
        self.assertEqual(self.history_for(self.alice, "mentor")[0].user_id, self.alice.id)

    def test_other_users_files_are_never_listed(self):
        self.post_application(self.alice_client, "mentor", files=[pdf_file("alice-secret.pdf")])
        body = self.bob_client.get("/api/me/applications").get_json()
        self.assertEqual(body["applications"]["mentor"]["credentials"], [])
        self.assertEqual(body["legacy_credentials"], [])
        self.assertNotIn("alice-secret", str(body))

    def test_one_users_submission_does_not_change_another_users_rejected_application(self):
        self.add_application(self.alice, "mentor", "rejected", review_note="No.")
        self.post_application(self.bob_client, "mentor")
        row = self.get_row(self.alice, "mentor")
        self.assertEqual(row.status, "rejected")
        self.assertEqual(row.review_note, "No.")
        self.assertEqual(self.history_for(self.alice, "mentor"), [])


# ---------------------------------------------------------------------------
class FailureHandlingTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user("fail@example.test", ["student"])
        self.client_a = self.client_for(self.user)

    def three_files(self):
        return [pdf_file("a.pdf"), pdf_file("b.pdf"), pdf_file("c.pdf")]

    def test_storage_failure_rolls_back_and_cleans_up_every_intended_object(self):
        bucket = self.use_bucket(FakeBucket(fail_on=2))
        res = self.post_application(self.client_a, "mentor", files=self.three_files())
        self.assertEqual(res.status_code, 500)
        self.assertFalse(res.get_json()["ok"])
        self.assertNotIn("simulated", res.get_data(as_text=True))

        self.assertEqual(self.counts(), (0, 0, 0))
        self.assertEqual(len(bucket.attempts), 2)          # third upload never attempted
        self.assertEqual(len(bucket.removed), 3)           # all three intended objects
        self.assertTrue(set(bucket.attempts) <= set(bucket.removed))
        self.assertEqual(bucket.stored, {})

    def test_database_commit_failure_rolls_back_and_removes_uploaded_objects(self):
        bucket = self.use_bucket(FakeBucket())
        with mock.patch.object(db.session, "commit", side_effect=RuntimeError("db down")):
            res = self.post_application(self.client_a, "mentor", files=self.three_files())
        self.assertEqual(res.status_code, 500)
        self.assertNotIn("db down", res.get_data(as_text=True))

        self.assertEqual(self.counts(), (0, 0, 0))
        self.assertEqual(len(bucket.attempts), 3)
        self.assertEqual(sorted(bucket.removed), sorted(bucket.attempts))
        self.assertEqual(bucket.stored, {})

    def test_failed_resubmission_leaves_the_previous_decision_intact(self):
        row = self.add_application(
            self.user, "mentor", "rejected", bio="Original description. " * 3,
            review_note="Add details", reviewed_by=7, reviewed_at=naive_utc_now(),
        )
        original_bio = row.bio
        bucket = self.use_bucket(FakeBucket())
        with mock.patch.object(db.session, "commit", side_effect=RuntimeError("db down")):
            res = self.post_application(self.client_a, "mentor", files=[pdf_file()])
        self.assertEqual(res.status_code, 500)

        row = self.get_row(self.user, "mentor")
        self.assertEqual(row.status, "rejected")
        self.assertEqual(row.bio, original_bio)
        self.assertEqual(row.review_note, "Add details")
        self.assertEqual(row.reviewed_by, 7)
        self.assertEqual(self.history_for(self.user, "mentor"), [])
        self.assertEqual(Credential.query.count(), 0)
        self.assertEqual(bucket.stored, {})

    def test_concurrent_duplicate_is_reported_as_conflict_and_cleaned_up(self):
        bucket = self.use_bucket(FakeBucket())
        error = IntegrityError("INSERT", {}, Exception("duplicate"))
        with mock.patch.object(db.session, "flush", side_effect=error):
            res = self.post_application(self.client_a, "mentor", files=[pdf_file()])
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["field"], "status")
        self.assertEqual(self.counts(), (0, 0, 0))
        self.assertEqual(len(bucket.removed), 1)
        self.assertEqual(bucket.stored, {})

    def test_unconfigured_storage_with_files_fails_cleanly(self):
        self.bucket_mock.side_effect = RuntimeError("Supabase Storage credentials are not configured.")
        res = self.post_application(self.client_a, "mentor", files=[pdf_file()])
        self.assertEqual(res.status_code, 500)
        self.assertNotIn("credentials are not configured", res.get_data(as_text=True))
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_zero_file_database_failure_never_touches_storage(self):
        with mock.patch.object(db.session, "commit", side_effect=RuntimeError("db down")):
            res = self.post_application(self.client_a, "mentor")
        self.assertEqual(res.status_code, 500)
        self.assertEqual(self.counts(), (0, 0, 0))
        self.assert_storage_untouched()

    def test_cleanup_failure_is_best_effort_and_still_returns_json(self):
        bucket = self.use_bucket(FakeBucket(fail_on=1, remove_raises=True))
        res = self.post_application(self.client_a, "mentor", files=[pdf_file()])
        self.assertEqual(res.status_code, 500)
        self.assertFalse(res.get_json()["ok"])
        self.assertEqual(len(bucket.removed), 1)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_error_responses_do_not_leak_paths_or_storage_details(self):
        bucket = self.use_bucket(FakeBucket(fail_on=1))
        res = self.post_application(self.client_a, "mentor", files=[pdf_file()])
        text = res.get_data(as_text=True).lower()
        self.assertNotIn("supabase", text)
        self.assertNotIn("bucket", text)
        for path in bucket.attempts:
            self.assertNotIn(path, text)
        self.assertNotIn(f"{self.user.id}/mentor/", text)


# ---------------------------------------------------------------------------
class HistoryTests(BaseCase):
    def test_first_submission_is_recorded(self):
        user = self.make_user("first@example.test", ["student"])
        self.post_application(self.client_for(user), "mentor")
        history = self.history_for(user, "mentor")
        self.assertEqual(len(history), 1)
        entry = history[0]
        self.assertEqual(entry.application_id, self.get_row(user, "mentor").id)
        self.assertEqual(entry.user_id, user.id)
        self.assertEqual(entry.event_type, "submitted")
        self.assertEqual(entry.from_status, "not_submitted")
        self.assertEqual(entry.to_status, "pending")
        for column in ("prior_bio", "prior_links", "prior_review_note", "prior_reviewed_by",
                       "prior_reviewed_at", "prior_submitted_at"):
            self.assertIsNone(getattr(entry, column), column)
        self.assertIsNotNone(entry.created_at)

    def test_history_preserves_every_prior_decision_and_submission(self):
        user = self.make_user("cycles@example.test", ["student"])
        client = self.client_for(user)
        texts = [MENTOR_TEXT, MENTOR_TEXT + " Second.", MENTOR_TEXT + " Third."]
        self.post_application(client, "mentor", description=texts[0])

        for i, status in enumerate(("rejected", "revision_required")):
            row = self.get_row(user, "mentor")
            row.status = status
            row.review_note = f"decision {i}"
            row.reviewed_by = 40 + i
            row.reviewed_at = naive_utc_now()
            db.session.commit()
            self.assertEqual(
                self.post_application(client, "mentor", description=texts[i + 1]).status_code, 200
            )

        history = self.history_for(user, "mentor")
        self.assertEqual(
            [(h.event_type, h.from_status) for h in history],
            [("submitted", "not_submitted"), ("resubmitted", "rejected"),
             ("resubmitted", "revision_required")],
        )
        self.assertEqual([h.prior_review_note for h in history], [None, "decision 0", "decision 1"])
        self.assertEqual([h.prior_reviewed_by for h in history], [None, 40, 41])
        self.assertEqual([h.prior_bio for h in history], [None, texts[0], texts[1]])
        self.assertEqual(self.get_row(user, "mentor").bio, texts[2])

    def test_resubmitting_a_legacy_row_preserves_its_content(self):
        user = self.make_user("legacy-history@example.test", ["student"])
        self.add_application(
            user, "coach", "rejected", bio="Legacy description. " * 4,
            links="https://example.test/legacy", review_note="Legacy note",
        )
        self.post_application(self.client_for(user), "coach", description=COACH_TEXT)
        entry = self.history_for(user, "coach")[0]
        self.assertEqual(entry.prior_bio, "Legacy description. " * 4)
        self.assertEqual(entry.prior_links, "https://example.test/legacy")
        self.assertEqual(entry.prior_review_note, "Legacy note")
        self.assertIsNotNone(entry.prior_submitted_at)  # falls back to created_at

    def test_history_is_kept_separately_for_each_role(self):
        user = self.make_user("separate@example.test", ["student"])
        client = self.client_for(user)
        self.post_application(client, "mentor")
        self.post_application(client, "coach", description=COACH_TEXT)
        mentor = self.history_for(user, "mentor")
        coach = self.history_for(user, "coach")
        self.assertEqual((len(mentor), len(coach)), (1, 1))
        self.assertNotEqual(mentor[0].application_id, coach[0].application_id)

    def test_history_rows_cannot_be_updated_or_deleted_through_the_orm(self):
        user = self.make_user("append-only@example.test", ["student"])
        self.post_application(self.client_for(user), "mentor")
        entry = RoleApplicationHistory.query.one()

        entry.prior_bio = "tampered"
        with self.assertRaises(ValueError):
            db.session.commit()
        db.session.rollback()

        db.session.delete(db.session.get(RoleApplicationHistory, entry.id))
        with self.assertRaises(ValueError):
            db.session.commit()
        db.session.rollback()

        self.assertEqual(RoleApplicationHistory.query.count(), 1)
        self.assertIsNone(RoleApplicationHistory.query.one().prior_bio)


# ---------------------------------------------------------------------------
class PrivilegeTests(BaseCase):
    """Submitting or resubmitting never grants provider privileges."""

    def test_submission_and_resubmission_never_change_roles_or_unlock_provider_access(self):
        user = self.make_user("priv@example.test", ["student"])
        client = self.client_for(user)
        self.use_bucket(FakeBucket())

        for role, text in (("mentor", MENTOR_TEXT), ("coach", COACH_TEXT)):
            self.assertEqual(
                self.post_application(client, role, description=text, files=[pdf_file()]).status_code,
                201,
            )
            row = self.get_row(user, role)
            row.status = "rejected"
            db.session.commit()
            self.assertEqual(
                self.post_application(client, role, description=text + " Better.").status_code, 200
            )

        self.assertEqual(db.session.get(User, user.id).roles, ["student"])
        self.assertEqual(client.get("/api/me").get_json()["user"]["roles"], ["student"])
        self.assertEqual(client.get("/schedule").status_code, 403)
        self.assertEqual(client.get("/api/schedule").status_code, 403)
        self.assertEqual(client.get("/api/availability").status_code, 403)
        self.assertEqual(client.get(f"/tutors/{user.id}").status_code, 404)

    def test_applicant_still_cannot_accept_provider_bookings(self):
        applicant = self.make_user("applicant@example.test", ["student"])
        learner = self.make_user("learner@example.test", ["student"])
        client = self.client_for(applicant)
        self.post_application(client, "mentor")

        appt = Appointment(
            provider_id=applicant.id, student_id=learner.id, role="mentor",
            topic="Algebra", start_time=naive_utc_now() + timedelta(hours=24),
            duration_minutes=60, status="pending",
        )
        db.session.add(appt)
        db.session.commit()

        self.assertEqual(client.post(f"/api/bookings/{appt.id}/accept").status_code, 403)
        db.session.refresh(appt)
        self.assertEqual(appt.status, "pending")


# ---------------------------------------------------------------------------
class CompatibilityTests(BaseCase):
    """Existing admin, status and Batch 2.1A behaviour is unchanged."""

    def test_admin_list_response_shape_is_unchanged(self):
        user = self.make_user("shape@example.test", ["student"])
        admin = self.make_user("shape-admin@example.test", ["student"], admin=True)
        self.use_bucket(FakeBucket())
        self.post_application(self.client_for(user), "mentor", files=[pdf_file("a.pdf")])

        body = self.client_for(admin).get("/api/admin/applications").get_json()
        self.assertEqual(set(body), {"ok", "applications"})
        item = body["applications"][0]
        self.assertEqual(
            set(item),
            {"id", "role", "status", "bio", "links", "submitted_at", "user", "credentials"},
        )
        self.assertEqual(set(item["user"]), {"id", "name", "email"})
        self.assertEqual(set(item["credentials"][0]), {"id", "name", "url"})
        self.assertEqual(item["bio"], MENTOR_TEXT)

    def test_admin_list_keeps_showing_legacy_files_on_every_application(self):
        user = self.make_user("legacy-admin@example.test", ["student"])
        admin = self.make_user("legacy-admin-reviewer@example.test", ["student"], admin=True)
        mentor = self.add_application(user, "mentor", "pending")
        self.add_application(user, "coach", "pending")
        db.session.add(Credential(
            user_id=user.id, original_name="legacy.pdf", stored_name="legacy/a.pdf"
        ))
        db.session.add(Credential(
            user_id=user.id, original_name="new-mentor.pdf", stored_name="1/mentor/x.pdf",
            application_id=mentor.id,
        ))
        db.session.commit()

        apps = self.client_for(admin).get("/api/admin/applications").get_json()["applications"]
        by_role = {a["role"]: sorted(c["name"] for c in a["credentials"]) for a in apps}
        self.assertEqual(by_role["mentor"], ["legacy.pdf", "new-mentor.pdf"])
        self.assertEqual(by_role["coach"], ["legacy.pdf"])
        # Legacy rows are still unassigned.
        legacy = Credential.query.filter_by(original_name="legacy.pdf").one()
        self.assertIsNone(legacy.application_id)

    def test_admin_list_uses_created_at_for_legacy_rows(self):
        user = self.make_user("legacy-time@example.test", ["student"])
        admin = self.make_user("legacy-time-admin@example.test", ["student"], admin=True)
        row = self.add_application(user, "mentor", "pending")
        item = self.client_for(admin).get("/api/admin/applications").get_json()["applications"][0]
        self.assertEqual(item["submitted_at"], row.created_at.isoformat())

    def test_verification_status_endpoint_still_reports_pending_roles(self):
        user = self.make_user("status@example.test", ["student"])
        client = self.client_for(user)
        self.post_application(client, "mentor")
        body = client.get("/api/me/verification-status").get_json()
        self.assertEqual(body["pending_roles"], ["mentor"])
        self.assertEqual(body["approved_roles"], [])
        self.assertEqual(body["rejected_roles"], [])

    def test_dashboard_shows_pending_banner_after_submission(self):
        user = self.make_user("banner@example.test", ["student"])
        client = self.client_for(user)
        self.assertNotIn(b"pending-verify-banner", client.get("/dashboard").data)
        self.post_application(client, "mentor")
        res = client.get("/dashboard")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"pending-verify-banner", res.data)

    def test_registration_still_creates_a_student_without_storage_or_applications(self):
        res = self.client.post("/api/auth/register", data={
            "name": "Plain Student",
            "email": "plain@example.test",
            "password": PASSWORD,
            "confirm_password": PASSWORD,
            "acknowledged": "true",
        })
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.get_json()["user"]["roles"], ["student"])
        self.assertEqual(self.counts(), (0, 0, 0))
        self.assert_storage_untouched()

    def test_legacy_provider_only_account_can_still_log_in(self):
        user = self.make_user("legacy-login@example.test", [])
        self.add_application(user, "mentor", "pending")
        res = self.login(self.app.test_client(), user.email)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(db.session.get(User, user.id).roles, [])


# ---------------------------------------------------------------------------
class CsrfTests(BaseCase):
    CSRF = True

    def test_post_requires_a_csrf_token(self):
        user = self.make_user("csrf@example.test", ["student"])
        client = self.app.test_client()

        token = client.get("/api/csrf-token").get_json()["csrf_token"]
        res = client.post(
            "/api/auth/login",
            json={"email": user.email, "password": PASSWORD},
            headers={"X-CSRFToken": token},
        )
        self.assertEqual(res.status_code, 200)

        refused = self.post_application(client, "mentor")
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(RoleApplication.query.count(), 0)

        token = client.get("/api/csrf-token").get_json()["csrf_token"]
        accepted = self.post_application(client, "mentor", headers={"X-CSRFToken": token})
        self.assertEqual(accepted.status_code, 201)
        self.assertEqual(RoleApplication.query.count(), 1)
        self.assert_storage_untouched()


# ---------------------------------------------------------------------------
class SchemaAndMigrationTests(unittest.TestCase):
    """Static checks only: migration files are read as text, never executed."""

    @classmethod
    def revisions(cls):
        found = {}
        for path in sorted(VERSIONS_DIR.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            rev = re.search(r"^revision\s*=\s*['\"]([0-9a-f]+)['\"]", text, re.M)
            down = re.search(r"^down_revision\s*=\s*(None|['\"]([0-9a-f]+)['\"])", text, re.M)
            if rev and down:
                found[rev.group(1)] = down.group(2)  # None for the baseline
        return found

    def test_new_revision_is_chained_after_the_current_head(self):
        revisions = self.revisions()
        self.assertIn("0720c7d1e4ca", revisions)
        self.assertIsNone(revisions["0720c7d1e4ca"])
        self.assertEqual(revisions.get("9e45f7e7aa32"), "0720c7d1e4ca")
        self.assertEqual(revisions.get("a6d2f9c83b10"), "f4c2a8d91e70")

    def test_migration_history_keeps_existing_merge_and_one_head(self):
        import ast
        revisions = {}
        for path in VERSIONS_DIR.glob("*.py"):
            values = {}
            for node in ast.parse(path.read_text(encoding="utf-8-sig")).body:
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id in ("revision", "down_revision"):
                            values[target.id] = ast.literal_eval(node.value)
            revisions[values["revision"]] = values["down_revision"]
        parents = {p for value in revisions.values() for p in (value if isinstance(value, tuple) else (value,)) if p}
        self.assertEqual(set(revisions) - parents, {"83ca40c9f279"})
        self.assertEqual(revisions["e6a1c9d84b20"], ("c1a82b7d43ef", "d2b7a4e9310c"))
        self.assertEqual(len(revisions), 19)

    def test_role_application_unique_constraint_is_intact(self):
        uniques = [
            tuple(sorted(col.name for col in c.columns))
            for c in RoleApplication.__table__.constraints
            if isinstance(c, UniqueConstraint)
        ]
        self.assertIn(("role", "user_id"), uniques)

    def test_new_foreign_keys_and_indexes_have_explicit_names(self):
        credential_fks = {fk.name for fk in Credential.__table__.foreign_keys}
        self.assertIn("fk_credentials_application_id", credential_fks)
        history_fks = {fk.name for fk in RoleApplicationHistory.__table__.foreign_keys}
        self.assertEqual(
            history_fks,
            {"fk_role_application_history_application_id", "fk_role_application_history_user_id", "fk_role_application_history_actor_id"},
        )
        credential_indexes = {i.name for i in Credential.__table__.indexes}
        self.assertIn("ix_credentials_application_id", credential_indexes)
        history_indexes = {i.name for i in RoleApplicationHistory.__table__.indexes}
        self.assertEqual(
            history_indexes,
            {"ix_role_application_history_application_id", "ix_role_application_history_user_id"},
        )

    def test_credential_application_id_is_nullable(self):
        self.assertTrue(Credential.__table__.c.application_id.nullable)
        self.assertTrue(RoleApplication.__table__.c.submitted_at.nullable)


if __name__ == "__main__":
    unittest.main()
