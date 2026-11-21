"""Isolated provider security regressions; no migrations or external services."""
from io import BytesIO
from pathlib import Path
from unittest import mock
from PIL import Image
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable
from extensions import db
from models import Credential, RoleApplication, RoleApplicationHistory
from test_provider_applications import BaseCase, FakeBucket, pdf_file


class ProviderSecurityTests(BaseCase):
    def setup_user(self):
        user = self.make_user("security@example.test", ["student"])
        return user, self.client_for(user)

    def test_invalid_images_never_reach_storage(self):
        user, client = self.setup_user()
        for content, name in [(b"\xff\xd8\xffgarbage", "bad.jpg"),
                              (b"\x89PNG\r\n\x1a\n" + b"0"*32, "bad.png")]:
            with self.subTest(name=name):
                result = self.post_application(client, "mentor", files=[(BytesIO(content), name)])
                self.assertEqual(result.status_code, 400)
        self.assert_storage_untouched()
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_valid_decoded_images_and_storage_outside_transactions(self):
        user, client = self.setup_user()
        self.assertEqual(client.get("/api/me").json["user"]["currency_symbol"], "\u20b1")
        db.session.rollback()
        bucket = self.use_bucket(FakeBucket())
        upload = bucket.upload
        def checked_upload(*args, **kwargs):
            self.assertFalse(db.session().in_transaction())
            return upload(*args, **kwargs)
        bucket.upload = checked_upload
        files = []
        for fmt, ext in [("PNG", "png"), ("JPEG", "jpg")]:
            content = BytesIO()
            Image.new("RGB", (8, 8)).save(content, format=fmt)
            content.seek(0)
            files.append((content, "proof." + ext))
        self.assertEqual(self.post_application(client, "mentor", files=files).status_code, 201)
        self.assertEqual(len(bucket.stored), 2)

    def test_acknowledgement_loss_does_not_delete_committed_evidence(self):
        user, client = self.setup_user()
        bucket = self.use_bucket(FakeBucket())
        commit = db.session.commit
        def committed_then_failed():
            commit()
            raise RuntimeError("simulated acknowledgement loss")
        with mock.patch.object(db.session, "commit", side_effect=committed_then_failed):
            result = self.post_application(client, "mentor", files=[pdf_file()])
        self.assertEqual(result.status_code, 500)
        self.assertEqual(self.counts(), (1, 1, 1))
        self.assertEqual(bucket.removed, [])
        self.assertEqual(len(bucket.stored), 1)
        self.assertEqual(db.session.get(type(user), user.id).roles, ["student"])

    def test_cumulative_evidence_limit_is_rechecked_after_upload(self):
        user, client = self.setup_user()
        row = self.add_application(user, "mentor", "rejected")
        app_id, user_id = row.id, user.id
        for i in range(4):
            db.session.add(Credential(user_id=user_id, application_id=app_id,
                original_name=f"old-{i}.pdf", stored_name=f"old-{i}.pdf"))
        db.session.commit()
        bucket = self.use_bucket(FakeBucket())
        upload = bucket.upload
        def racing_upload(*args, **kwargs):
            result = upload(*args, **kwargs)
            db.session.add(Credential(user_id=user_id, application_id=app_id,
                original_name="concurrent.pdf", stored_name="concurrent.pdf"))
            db.session.commit()
            return result
        bucket.upload = racing_upload
        result = self.post_application(client, "mentor", files=[pdf_file()])
        self.assertEqual(result.status_code, 409)
        self.assertEqual(Credential.query.count(), 5)
        self.assertEqual(RoleApplication.query.one().status, "rejected")
        self.assertEqual(RoleApplicationHistory.query.count(), 0)
        self.assertEqual(bucket.stored, {})

    def test_actor_and_decision_notes_are_permanent(self):
        user, client = self.setup_user()
        self.post_application(client, "mentor")
        first = RoleApplicationHistory.query.one()
        self.assertEqual(first.actor_id, user.id)
        admin = self.make_user("reviewer@example.test", [], admin=True)
        row_id = RoleApplication.query.one().id
        result = self.client_for(admin).post(f"/api/admin/applications/{row_id}/request-revision",
            json={"note":"Please provide more specific teaching experience."})
        self.assertEqual(result.status_code, 200)
        event = RoleApplicationHistory.query.order_by(RoleApplicationHistory.id.desc()).first()
        self.assertEqual(event.actor_id, admin.id)
        self.assertEqual(event.decision_note, "Please provide more specific teaching experience.")
        for statement in ["UPDATE role_application_history SET decision_note='changed'", "DELETE FROM role_application_history"]:
            with self.subTest(statement=statement), self.assertRaises(DatabaseError):
                db.session.execute(text(statement))
            db.session.rollback()
        self.assertEqual(RoleApplicationHistory.query.count(), 2)

    def test_new_history_security_is_defined_without_running_migration(self):
        source = (Path(__file__).resolve().parents[1] / "migrations/versions/a6d2f9c83b10_provider_applications_and_history.py").read_text()
        for required in ["f4c2a8d91e70", "ENABLE ROW LEVEL SECURITY", "REVOKE ALL", "role_application_history_no_truncate", "raise RuntimeError"]:
            self.assertIn(required, source)
        ddl = str(CreateTable(RoleApplicationHistory.__table__).compile(dialect=postgresql.dialect()))
        self.assertIn("actor_id", ddl)
        self.assertIn("decision_note", ddl)
        self.assertIn("REFERENCES users", ddl)
