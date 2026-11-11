import io
import os
import unittest
from unittest.mock import MagicMock, patch

from __init__ import create_app
from extensions import db
from models import User
from routes import (
    AVATARS_BUCKET,
    MAX_AVATAR_BYTES,
    extract_supabase_avatar_path,
    validate_avatar_file,
)
from werkzeug.datastructures import FileStorage


class Phase5BAvatarStorageTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SUPABASE_URL="https://testref.supabase.co",
            SUPABASE_SERVICE_ROLE_KEY="test-service-key",
        )
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

        self.user = User(
            display_name="Avatar Test User",
            email="avataruser@example.test",
            password_hash="test",
            roles_json='["student"]',
        )
        db.session.add(self.user)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    # --- Unit Tests: validate_avatar_file ---

    def test_validate_avatar_file_success_all_formats(self):
        """Test that valid JPG, PNG, and WebP files pass magic-byte validation."""
        valid_samples = [
            ("photo.jpg", b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01", "jpg"),
            ("photo.jpeg", b"\xff\xd8\xff\xdb\x00\x43\x00", "jpeg"),
            ("graphic.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR", "png"),
            ("sticker.webp", b"RIFF\x20\x00\x00\x00WEBPVP8 \x10\x00\x00\x00", "webp"),
        ]
        for filename, data, expected_ext in valid_samples:
            fs = FileStorage(stream=io.BytesIO(data), filename=filename)
            ext, size, err = validate_avatar_file(fs)
            self.assertIsNone(err, f"Expected {filename} to be valid, got error: {err}")
            self.assertEqual(ext, expected_ext)
            self.assertEqual(size, len(data))

    def test_validate_avatar_file_invalid_extension(self):
        """Test rejection of unsupported file extensions."""
        for filename in ["doc.pdf", "code.exe", "data.json", "script.sh", "noextension"]:
            fs = FileStorage(stream=io.BytesIO(b"dummy data"), filename=filename)
            ext, size, err = validate_avatar_file(fs)
            self.assertIsNone(ext)
            self.assertIn("Only JPG, PNG, and WebP images are allowed.", err)

    def test_validate_avatar_file_invalid_magic_bytes(self):
        """Test rejection of spoofed image extensions with invalid file headers."""
        spoofed = [
            ("fake.jpg", b"This is just plain text content."),
            ("fake.png", b"GIF89a1234567890"),
            ("fake.webp", b"RIFF\x00\x00\x00\x00AVI LIST"),
        ]
        for filename, data in spoofed:
            fs = FileStorage(stream=io.BytesIO(data), filename=filename)
            ext, size, err = validate_avatar_file(fs)
            self.assertIsNone(ext)
            self.assertIn("does not appear to be a valid", err)

    def test_validate_avatar_file_empty(self):
        """Test rejection of 0-byte empty file."""
        fs = FileStorage(stream=io.BytesIO(b""), filename="empty.jpg")
        ext, size, err = validate_avatar_file(fs)
        self.assertIsNone(ext)
        self.assertIn("empty", err.lower())

    def test_validate_avatar_file_exceeds_max_size(self):
        """Test rejection of file exceeding 2 MB size limit."""
        # Valid JPG header followed by > 2MB data
        large_bytes = b"\xff\xd8\xff\xe0" + (b"\x00" * (MAX_AVATAR_BYTES + 10))
        fs = FileStorage(stream=io.BytesIO(large_bytes), filename="huge.jpg")
        ext, size, err = validate_avatar_file(fs)
        self.assertIsNone(ext)
        self.assertIn("must not exceed 2 MB", err)

    # --- Unit Tests: extract_supabase_avatar_path ---

    def test_extract_supabase_avatar_path(self):
        """Test extracting object path only for matching Supabase avatars and user ID."""
        user_id = self.user.id
        matching_url = f"https://testref.supabase.co/storage/v1/object/public/{AVATARS_BUCKET}/user_{user_id}/photo123.jpg"
        self.assertEqual(
            extract_supabase_avatar_path(matching_url, user_id),
            f"user_{user_id}/photo123.jpg",
        )

        # Different user
        diff_user_url = f"https://testref.supabase.co/storage/v1/object/public/{AVATARS_BUCKET}/user_{user_id + 99}/photo123.jpg"
        self.assertIsNone(extract_supabase_avatar_path(diff_user_url, user_id))

        # Credentials bucket (should be rejected)
        cred_url = f"https://testref.supabase.co/storage/v1/object/public/credentials/user_{user_id}/doc.jpg"
        self.assertIsNone(extract_supabase_avatar_path(cred_url, user_id))

        # Local avatar path (should be rejected)
        local_url = f"/static/uploads/avatars/user_{user_id}_legacy.jpg"
        self.assertIsNone(extract_supabase_avatar_path(local_url, user_id))

        # External URL (should be rejected)
        ext_url = f"https://lh3.googleusercontent.com/storage/v1/object/public/{AVATARS_BUCKET}/user_{user_id}/photo.jpg"
        self.assertIsNone(extract_supabase_avatar_path(ext_url, user_id))

        # Directory traversal attempt
        traversal_url = f"https://testref.supabase.co/storage/v1/object/public/{AVATARS_BUCKET}/user_{user_id}/../admin/secret.jpg"
        self.assertIsNone(extract_supabase_avatar_path(traversal_url, user_id))

        # None or empty
        self.assertIsNone(extract_supabase_avatar_path(None, user_id))
        self.assertIsNone(extract_supabase_avatar_path("", user_id))

    # --- Integration Tests: POST /api/profile/avatar ---

    def test_upload_avatar_unauthenticated(self):
        """Test that unauthenticated requests are redirected or denied."""
        client = self.app.test_client()
        res = client.post("/api/profile/avatar")
        self.assertIn(res.status_code, (302, 401))

    def test_upload_avatar_no_file(self):
        """Test POST with missing 'avatar' form field."""
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.id)
            sess["_fresh"] = True

        res = client.post("/api/profile/avatar", data={}, content_type="multipart/form-data")
        self.assertEqual(res.status_code, 400)
        data = res.get_json()
        self.assertFalse(data["ok"])
        self.assertIn("No image file provided", data["message"])

    def test_upload_avatar_validation_failure(self):
        """Test POST with corrupted image file fails validation."""
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.id)
            sess["_fresh"] = True

        bad_file = (io.BytesIO(b"Not a real image"), "corrupt.jpg")
        res = client.post(
            "/api/profile/avatar",
            data={"avatar": bad_file},
            content_type="multipart/form-data",
        )
        self.assertEqual(res.status_code, 400)
        data = res.get_json()
        self.assertFalse(data["ok"])
        self.assertIn("does not appear to be a valid", data["message"])

    @patch("routes.avatars_bucket")
    def test_upload_avatar_success(self, mock_avatars_bucket):
        """Test successful avatar upload to Supabase Storage and DB record update."""
        mock_bucket = MagicMock()
        mock_avatars_bucket.return_value = mock_bucket

        def fake_get_public_url(path):
            return f"https://testref.supabase.co/storage/v1/object/public/{AVATARS_BUCKET}/{path}"

        mock_bucket.get_public_url.side_effect = fake_get_public_url

        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.id)
            sess["_fresh"] = True

        valid_jpg = (io.BytesIO(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01imagebytes"), "my_photo.jpg")
        res = client.post(
            "/api/profile/avatar",
            data={"avatar": valid_jpg},
            content_type="multipart/form-data",
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["ok"])
        self.assertIn("Avatar uploaded successfully", data["message"])

        # Check DB update
        updated_user = db.session.get(User, self.user.id)
        self.assertTrue(updated_user.profile_picture.startswith(
            f"https://testref.supabase.co/storage/v1/object/public/{AVATARS_BUCKET}/user_{self.user.id}/"
        ))
        self.assertTrue(updated_user.profile_picture.endswith(".jpg"))

        # Verify upload call options
        mock_bucket.upload.assert_called_once()
        call_kwargs = mock_bucket.upload.call_args[1]
        self.assertTrue(call_kwargs["path"].startswith(f"user_{self.user.id}/"))
        self.assertEqual(call_kwargs["file_options"]["content-type"], "image/jpeg")

    @patch("routes.avatars_bucket")
    def test_upload_avatar_storage_failure(self, mock_avatars_bucket):
        """Test storage failure preserves original avatar and returns 503."""
        mock_bucket = MagicMock()
        mock_avatars_bucket.return_value = mock_bucket
        mock_bucket.upload.side_effect = RuntimeError("Storage service down")

        original_pic = "/static/uploads/avatars/user_1_original.jpg"
        self.user.profile_picture = original_pic
        db.session.commit()

        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.id)
            sess["_fresh"] = True

        valid_png = (io.BytesIO(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRpngdata"), "photo.png")
        res = client.post(
            "/api/profile/avatar",
            data={"avatar": valid_png},
            content_type="multipart/form-data",
        )
        self.assertEqual(res.status_code, 503)
        data = res.get_json()
        self.assertFalse(data["ok"])

        # Check DB remained unchanged
        refreshed_user = db.session.get(User, self.user.id)
        self.assertEqual(refreshed_user.profile_picture, original_pic)

    @patch("routes.cleanup_avatar_object")
    @patch("routes.avatars_bucket")
    def test_upload_avatar_db_commit_failure_rollback(self, mock_avatars_bucket, mock_cleanup):
        """Test database commit failure rolls back DB and cleans up newly uploaded storage object."""
        mock_bucket = MagicMock()
        mock_avatars_bucket.return_value = mock_bucket
        mock_bucket.get_public_url.return_value = "https://testref.supabase.co/storage/v1/object/public/avatars/user_1/new.jpg"

        original_pic = "/static/uploads/avatars/user_1_original.jpg"
        self.user.profile_picture = original_pic
        db.session.commit()

        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.id)
            sess["_fresh"] = True

        with patch("extensions.db.session.commit", side_effect=RuntimeError("DB commit failed")):
            valid_jpg = (io.BytesIO(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01data"), "photo.jpg")
            res = client.post(
                "/api/profile/avatar",
                data={"avatar": valid_jpg},
                content_type="multipart/form-data",
            )
            self.assertEqual(res.status_code, 500)
            data = res.get_json()
            self.assertFalse(data["ok"])

            # Cleanup should have been called on the newly uploaded path
            mock_cleanup.assert_called_once()
            cleaned_path = mock_cleanup.call_args[0][0]
            self.assertTrue(cleaned_path.startswith(f"user_{self.user.id}/"))

        # DB session rollback should preserve original avatar
        db.session.rollback()
        refreshed_user = db.session.get(User, self.user.id)
        self.assertEqual(refreshed_user.profile_picture, original_pic)

    @patch("routes.cleanup_avatar_object")
    @patch("routes.avatars_bucket")
    def test_upload_avatar_cleans_up_previous_supabase_avatar(self, mock_avatars_bucket, mock_cleanup):
        """Test that only previous Supabase avatars belonging to the user are cleaned up after commit."""
        old_supabase_url = f"https://testref.supabase.co/storage/v1/object/public/{AVATARS_BUCKET}/user_{self.user.id}/old_avatar.jpg"
        self.user.profile_picture = old_supabase_url
        db.session.commit()

        mock_bucket = MagicMock()
        mock_avatars_bucket.return_value = mock_bucket
        mock_bucket.get_public_url.return_value = f"https://testref.supabase.co/storage/v1/object/public/{AVATARS_BUCKET}/user_{self.user.id}/new_avatar.jpg"

        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.id)
            sess["_fresh"] = True

        valid_jpg = (io.BytesIO(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01data"), "photo.jpg")
        res = client.post(
            "/api/profile/avatar",
            data={"avatar": valid_jpg},
            content_type="multipart/form-data",
        )
        self.assertEqual(res.status_code, 200)

        # Cleanup should be invoked on old_avatar.jpg
        mock_cleanup.assert_called_once_with(f"user_{self.user.id}/old_avatar.jpg")

    @patch("routes.cleanup_avatar_object")
    @patch("routes.avatars_bucket")
    def test_upload_avatar_preserves_local_avatar_reference(self, mock_avatars_bucket, mock_cleanup):
        """Test that previous local avatars are NOT cleaned up from Supabase Storage."""
        local_avatar_url = f"/static/uploads/avatars/user_{self.user.id}_legacy.jpg"
        self.user.profile_picture = local_avatar_url
        db.session.commit()

        mock_bucket = MagicMock()
        mock_avatars_bucket.return_value = mock_bucket
        mock_bucket.get_public_url.return_value = f"https://testref.supabase.co/storage/v1/object/public/{AVATARS_BUCKET}/user_{self.user.id}/new_avatar.jpg"

        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.id)
            sess["_fresh"] = True

        valid_jpg = (io.BytesIO(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01data"), "photo.jpg")
        res = client.post(
            "/api/profile/avatar",
            data={"avatar": valid_jpg},
            content_type="multipart/form-data",
        )
        self.assertEqual(res.status_code, 200)

        # Cleanup should NOT have been called because previous avatar was a local file
        mock_cleanup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
