import io
import unittest
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from __init__ import create_app
from extensions import db
from models import Appointment, User, Payment, MentorAvailability, utcnow
from payments import new_payment, MAX_RECEIPT_BYTES

PNG = bytes.fromhex('89504e470d0a1a0a0000000d4948445200000001000000010804000000b51c0c020000000b4944415478da6364f80f00010501012718e3660000000049454e44ae426082')


class PaymentTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(dict(TESTING=True, WTF_CSRF_ENABLED=False, SQLALCHEMY_DATABASE_URI='sqlite://', SQLALCHEMY_ENGINE_OPTIONS={},
                                   PAYMENT_LEDGER_APPROVAL_ENABLED=True))
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.student = User(display_name='Student', email='student@test.local', password_hash='test')
        self.other = User(display_name='Other', email='other@test.local', password_hash='test')
        self.provider = User(display_name='Teacher', email='teacher@test.local', password_hash='test', roles_json='["mentor"]', teaching_rate=Decimal('123.45'))
        self.admin = User(display_name='Admin', email='admin@test.local', password_hash='test', is_admin=True)
        db.session.add_all([self.student, self.other, self.provider, self.admin])
        db.session.flush()
        self.booking = Appointment(provider_id=self.provider.id, student_id=self.student.id, topic='Math', start_time=utcnow()+timedelta(days=2), status='pending', price=Decimal('123.45'), payment_required=True)
        db.session.add(self.booking)
        db.session.flush()
        self.payment = new_payment(self.booking)
        db.session.add(self.payment)
        db.session.commit()
        self.client = self.app.test_client()
        self.storage = MagicMock()
        self.mock = patch('payments.receipts_bucket', return_value=self.storage)
        self.mock.start()
        # SQLite regression tests exercise the existing receipt/review paths.
        # Real posting and activation are covered in the migrated PostgreSQL suite.
        for name, value in (("approval_ready", True), ("approval_complete", False), ("post_initial_allocation", None)):
            substitute = patch(f"payments.{name}", return_value=value)
            substitute.start()
            self.addCleanup(substitute.stop)
        self.login(self.student)

    def tearDown(self):
        self.mock.stop()
        db.session.remove()
        db.drop_all()
        self.context.pop()
        self.doCleanups()

    def login(self, user):
        from flask import g
        g.pop("_login_user", None)
        with self.client.session_transaction() as session:
            session['_user_id'] = str(user.id)
            session['_fresh'] = True

    def upload(self, content=PNG, name='proof.png', **extra):
        return self.client.post(f'/api/payments/bookings/{self.booking.id}/submit', data=dict(receipt=(io.BytesIO(content), name), transaction_reference='TX-123456', method='GCash', **extra))

    def review(self, action='approve', **data):
        self.login(self.admin)
        payload = {"confirmed": True, "expected_revision": self.payment.submission_revision} | data
        return self.client.post(f'/api/admin/payments/{self.payment.id}/{action}', json=payload)

    def test_owner_views_and_history(self):
        self.assertEqual(self.client.get(f'/payments/bookings/{self.booking.id}').status_code, 200)
        self.assertEqual(self.client.get('/payments').status_code, 200)
        response = self.client.get(f'/api/payments/bookings/{self.booking.id}')
        self.assertEqual(response.json['payment']['amount'], '123.45')
        self.assertIn('no-store', response.headers['Cache-Control'])

    def test_idor_all_student_endpoints(self):
        self.payment.receipt_path = 'private.png'
        db.session.commit()
        self.login(self.other)
        for url in (f'/payments/bookings/{self.booking.id}', f'/api/payments/bookings/{self.booking.id}', f'/payments/{self.payment.id}/receipt'):
            self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.upload().status_code, 404)
        self.storage.upload.assert_not_called()

    def test_submission_ignores_amount_and_status_and_duplicate(self):
        self.assertEqual(self.upload(amount='0.01', status='VERIFIED', student_id=str(self.other.id)).status_code, 200)
        self.assertEqual(self.payment.status, 'SUBMITTED')
        self.assertEqual(self.payment.amount, Decimal('123.45'))
        self.assertFalse(self.booking.payment_satisfied)
        self.assertEqual(self.upload().status_code, 409)
        self.assertEqual(self.storage.upload.call_count, 1)

    def test_invalid_files_and_size(self):
        for data, name, code in ((b'fake', 'x.exe', 400), (b'fake', 'x.png', 400), (b'\x89PNG\r\n\x1a\n', 'x.png', 400), (PNG, 'x.jpg', 400), (b'x'*(MAX_RECEIPT_BYTES+1), 'x.png', 413)):
            with self.subTest(name=name, code=code):
                self.assertEqual(self.upload(data, name).status_code, code)
        self.assertEqual(self.payment.status, 'PENDING')
        self.storage.upload.assert_not_called()

    def test_storage_failure_preserves_previous(self):
        self.payment.status = 'REJECTED'
        self.payment.receipt_path = 'old.png'
        db.session.commit()
        self.storage.upload.side_effect = RuntimeError('private failure')
        self.assertEqual(self.upload().status_code, 503)
        self.assertEqual(self.payment.receipt_path, 'old.png')
        self.assertEqual(self.payment.status, 'REJECTED')
        self.storage.remove.assert_not_called()

    def test_admin_approval_records_reviewer_preserves_acceptance(self):
        self.upload()
        self.login(self.admin)
        self.assertEqual(self.client.get('/admin/payments').status_code, 200)
        self.assertEqual(self.review().status_code, 200)
        self.assertEqual(self.payment.status, 'VERIFIED')
        self.assertEqual(self.payment.reviewed_by, self.admin.id)
        self.assertIsNotNone(self.payment.reviewed_at)
        self.assertEqual(self.booking.status, 'pending')
        self.assertTrue(self.booking.payment_satisfied)
        reviewed = self.payment.reviewed_at
        self.assertEqual(self.review().status_code, 409)
        self.assertEqual(self.review('reject', reason='Duplicate').status_code, 409)
        self.assertEqual(self.payment.reviewed_at, reviewed)

    def test_rejection_and_resubmission(self):
        self.upload()
        self.assertEqual(self.review('reject', reason='').status_code, 400)
        self.assertEqual(self.review('reject', reason='Transaction not received').status_code, 200)
        self.assertEqual(self.payment.status, 'REJECTED')
        self.assertEqual(self.payment.rejection_reason, 'Transaction not received')
        self.assertEqual(self.payment.reviewed_by, self.admin.id)
        self.assertFalse(self.booking.payment_satisfied)
        self.login(self.student)
        self.assertIn(b'Transaction not received', self.client.get(f'/payments/bookings/{self.booking.id}').data)
        old_path = self.payment.receipt_path
        self.assertEqual(self.upload().status_code, 200)
        self.assertNotEqual(self.payment.receipt_path, old_path)
        self.assertEqual(self.payment.status, 'SUBMITTED')
        self.storage.remove.assert_not_called()  # Retain receipts referenced by audit history.

    def test_non_admin_verification_denied(self):
        self.upload()
        for user in (self.student, self.other, self.provider):
            self.login(user)
            self.assertEqual(self.client.get('/admin/payments').status_code, 403)
            for action in ('approve', 'reject'):
                self.assertEqual(self.client.post(f'/api/admin/payments/{self.payment.id}/{action}', json={}).status_code, 403)
        self.assertEqual(self.payment.status, 'SUBMITTED')

    def test_join_all_payment_states_and_participant_checks(self):
        self.booking.status = 'accepted'
        self.booking.meeting_room = 'l2-test-payment'
        for status in ('PENDING', 'SUBMITTED', 'REJECTED', 'VERIFIED'):
            self.payment.status = status
            db.session.commit()
            for user in (self.student, self.provider):
                self.login(user)
                expected = 200 if status == 'VERIFIED' else 403
                self.assertEqual(self.client.get(f'/api/sessions/{self.booking.id}/join').status_code, expected)
                with patch('routes.create_jaas_participant_token', return_value='test-token') as token:
                    self.assertEqual(self.client.get(f'/sessions/{self.booking.id}/meeting').status_code, expected)
                    self.assertEqual(token.call_count, int(status == 'VERIFIED'))
        self.login(self.other)
        self.assertEqual(self.client.get(f'/api/sessions/{self.booking.id}/join').status_code, 404)
        self.assertEqual(self.client.get(f'/sessions/{self.booking.id}/meeting').status_code, 404)

    def test_provider_acceptance_unchanged(self):
        self.login(self.provider)
        response = self.client.post(f'/api/bookings/{self.booking.id}/accept')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.booking.status, 'accepted')
        self.assertTrue(self.booking.meeting_room)
        self.assertFalse(self.booking.payment_satisfied)

    def test_receipt_private_access(self):
        self.upload()
        self.storage.download.return_value = PNG
        for user in (self.student, self.admin):
            self.login(user)
            response = self.client.get(f'/payments/{self.payment.id}/receipt')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, PNG)
            self.assertIn('attachment', response.headers['Content-Disposition'])
        self.login(self.provider)
        self.assertEqual(self.client.get(f'/payments/{self.payment.id}/receipt').status_code, 404)

    def test_booking_creation_snapshots_rate(self):
        day = (utcnow()+timedelta(days=5)).date()
        db.session.add(MentorAvailability(mentor_id=self.provider.id, weekday=day.weekday(), start_time='09:00', end_time='17:00'))
        db.session.commit()
        response = self.client.post('/api/bookings', json=dict(mentor_id=self.provider.id, date=day.isoformat(), time='10:00', topic='Math', price='1'))
        self.assertEqual(response.status_code, 201)
        booking = db.session.get(Appointment, response.json['booking_id'])
        self.assertTrue(booking.payment_required)
        self.assertEqual(booking.payment.amount, Decimal('123.45'))
        self.assertNotEqual(booking.payment.reference, self.payment.reference)
        self.assertTrue(response.json['payment_url'])
        self.provider.teaching_rate = Decimal('999.00')
        db.session.commit()
        self.assertEqual(booking.payment.amount, Decimal('123.45'))

    def test_cancelled_booking_cannot_submit_or_review(self):
        self.booking.status = 'cancelled'
        db.session.commit()
        self.assertEqual(self.upload().status_code, 409)
        self.payment.status = 'SUBMITTED'
        db.session.commit()
        self.assertEqual(self.review().status_code, 409)

    def test_approval_confirmation_and_invalid_json(self):
        self.upload()
        self.login(self.admin)
        url = f'/api/admin/payments/{self.payment.id}/approve'
        self.assertEqual(self.client.post(url, json={}).status_code, 400)
        self.assertEqual(self.client.post(url, json=['bad']).status_code, 400)
        self.assertEqual(self.payment.status, 'SUBMITTED')

    def test_precommit_write_failure_compensates_upload(self):
        from sqlalchemy.exc import SQLAlchemyError
        with patch.object(db.session, 'flush', side_effect=SQLAlchemyError('test')):
            response = self.upload()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.payment.status, 'PENDING')
        self.assertIsNone(self.payment.receipt_path)
        self.storage.remove.assert_called_once()

    def test_lost_commit_acknowledgement_preserves_committed_receipt(self):
        from sqlalchemy.exc import SQLAlchemyError
        commit = db.session.commit

        def commit_then_lose_acknowledgement():
            commit()
            raise SQLAlchemyError("lost commit acknowledgement")

        with patch.object(db.session, "commit", side_effect=commit_then_lose_acknowledgement):
            response = self.upload()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.payment.status, "SUBMITTED")
        self.assertTrue(self.payment.receipt_path)
        self.assertEqual(self.payment.events[-1].receipt_path, self.payment.receipt_path)
        self.storage.remove.assert_not_called()

    def test_public_bucket_fails_closed(self):
        self.mock.stop()
        from payments import receipts_bucket
        try:
            client = MagicMock()
            client.storage.get_bucket.return_value = {'public': True}
            with patch('payments.create_client', return_value=client):
                with self.assertRaises(RuntimeError):
                    receipts_bucket()
                client.storage.from_.assert_not_called()
        finally:
            self.mock.start()

    def test_csrf_required(self):
        self.app.config['WTF_CSRF_ENABLED'] = True
        self.assertEqual(self.upload().status_code, 400)
        self.assertEqual(self.review().status_code, 400)

    def test_legacy_booking_remains_eligible(self):
        self.booking.payment_required = False
        self.booking.status = 'accepted'
        self.booking.meeting_room = 'legacy'
        db.session.commit()
        self.assertEqual(self.client.get(f'/api/sessions/{self.booking.id}/join').status_code, 200)

    def image_bytes(self, fmt="JPEG", size=(16, 16)):
        from PIL import Image
        output = io.BytesIO()
        with Image.new("RGB", size, "white") as image:
            image.save(output, format=fmt)
        return output.getvalue()

    def test_valid_decoded_jpeg(self):
        self.assertEqual(self.upload(self.image_bytes(), "receipt.jpeg").status_code, 200)
        self.assertEqual(self.storage.upload.call_args.args[1], self.image_bytes())

    def test_valid_decoded_png(self):
        self.assertEqual(self.upload(self.image_bytes("PNG"), "receipt.png").status_code, 200)

    def test_corrupt_and_truncated_image_data_never_reach_storage(self):
        import struct
        import zlib
        jpeg = self.image_bytes()
        # A valid PNG container/CRC with invalid compressed pixel data.
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
        bad_png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
                   + chunk(b"IDAT", b"not compressed image data") + chunk(b"IEND", b""))
        for content, name in ((b"\xff\xd8\xff" + b"garbage" * 5 + b"\xff\xd9", "bad.jpg"),
                              (bad_png, "bad.png"), (jpeg[:-2], "truncated.jpg"),
                              (jpeg[:len(jpeg)//2], "half.jpg"), (PNG[:-8], "truncated.png")):
            with self.subTest(name=name):
                response = self.upload(content, name)
                self.assertEqual(response.status_code, 400)
                self.assertNotIn("Traceback", response.get_data(as_text=True))
        self.storage.upload.assert_not_called()
        self.assertEqual(self.payment.status, "PENDING")

    def test_mime_and_extension_must_match_decoder(self):
        for content, name, mime in ((PNG, "receipt.jpg", "image/jpeg"),
                                    (self.image_bytes(), "receipt.png", "image/png"),
                                    (PNG, "receipt.png", "text/plain"),
                                    (self.image_bytes(), "receipt.jpg", "image/png"),
                                    (PNG, "receipt.webp", "image/webp")):
            with self.subTest(name=name, mime=mime):
                response = self.client.post(f"/api/payments/bookings/{self.booking.id}/submit", data={
                    "receipt": (io.BytesIO(content), name, mime), "method": "GCash", "transaction_reference": "TX-1234"})
                self.assertEqual(response.status_code, 400)
        self.storage.upload.assert_not_called()

    def test_dimension_and_pixel_limits(self):
        for size in ((8193, 1), (1, 8193), (4001, 4000)):
            with self.subTest(size=size):
                self.assertEqual(self.upload(self.image_bytes("PNG", size)).status_code, 400)
        self.storage.upload.assert_not_called()

    def test_animated_png_not_supported(self):
        from PIL import Image
        output = io.BytesIO()
        with Image.new("RGB", (2, 2), "white") as first, Image.new("RGB", (2, 2), "black") as second:
            first.save(output, format="PNG", save_all=True, append_images=[second])
        self.assertEqual(self.upload(output.getvalue()).status_code, 400)
        self.storage.upload.assert_not_called()

    def test_request_limit_precedes_csrf_and_multipart_parsing(self):
        from flask import Request
        self.app.config["WTF_CSRF_ENABLED"] = True
        with patch.object(Request, "_load_form_data", side_effect=AssertionError("multipart parsed")):
            response = self.upload(b"x" * (self.app.config["PAYMENT_REQUEST_MAX_BYTES"] + 1))
        self.assertEqual(response.status_code, 413)
        self.assertIn("5 MB", response.json["message"])
        self.storage.upload.assert_not_called()

    def test_streamed_request_without_content_length_is_bounded(self):
        response = self.client.post(f"/api/payments/bookings/{self.booking.id}/submit", data={
            "receipt": (io.BytesIO(b"x" * (7 * 1024 * 1024)), "receipt.png"),
            "method": "GCash", "transaction_reference": "TX-1234"},
            environ_overrides={"CONTENT_LENGTH": "", "wsgi.input_terminated": True})
        self.assertEqual(response.status_code, 413)
        self.storage.upload.assert_not_called()

    def test_global_request_limit_and_other_upload_budget(self):
        from flask import Request
        with patch.object(Request, "_load_form_data", side_effect=AssertionError("multipart parsed")):
            response = self.client.post("/api/profile/avatar", data=b"x" * (32 * 1024 * 1024 + 1), content_type="application/octet-stream")
        self.assertEqual(response.status_code, 413)
        # Receipt-specific 6 MiB ceiling does not apply to the credential route.
        with self.app.test_request_context("/api/auth/register", method="POST", data=b"x" * (26 * 1024 * 1024)):
            self.assertIsNone(self.app.preprocess_request())
            from flask import request
            self.assertEqual(request.max_content_length, 32 * 1024 * 1024)

    def test_storage_upload_runs_without_database_transaction(self):
        def upload(*args, **kwargs):
            self.assertFalse(db.session().in_transaction())
        self.storage.upload.side_effect = upload
        self.assertEqual(self.upload().status_code, 200)

    def test_changed_payment_during_upload_is_not_overwritten(self):
        payment_id = self.payment.id
        def upload(*args, **kwargs):
            self.assertFalse(db.session().in_transaction())
            winner = db.session.get(Payment, payment_id)
            winner.status = "SUBMITTED"
            winner.receipt_path = "winner.png"
            db.session.commit()
        self.storage.upload.side_effect = upload
        self.assertEqual(self.upload().status_code, 409)
        self.assertEqual(self.payment.receipt_path, "winner.png")
        self.storage.remove.assert_called_once_with([self.storage.upload.call_args.args[0]])

    def test_cancelled_during_upload_is_not_submitted(self):
        booking_id = self.booking.id
        def upload(*args, **kwargs):
            db.session.get(Appointment, booking_id).status = "cancelled"
            db.session.commit()
        self.storage.upload.side_effect = upload
        self.assertEqual(self.upload().status_code, 409)
        self.assertEqual(self.payment.status, "PENDING")
        self.assertIsNone(self.payment.receipt_path)
        self.storage.remove.assert_called_once()

    def test_cleanup_and_download_run_without_database_transaction(self):
        self.payment.status = "REJECTED"
        self.payment.receipt_path = "previous.png"
        db.session.commit()
        def remove(*args, **kwargs):
            self.assertFalse(db.session().in_transaction())
        self.storage.remove.side_effect = remove
        self.assertEqual(self.upload().status_code, 200)
        payment_id = self.payment.id
        def download(*args, **kwargs):
            self.assertFalse(db.session().in_transaction())
            return PNG
        self.storage.download.side_effect = download
        self.assertEqual(self.client.get(f"/payments/{payment_id}/receipt").status_code, 200)


if __name__ == '__main__':
    unittest.main()
