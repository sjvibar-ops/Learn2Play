import json
import os
import re
import unittest

from wsgi import app
from extensions import db
from models import User


class Phase4FrontendCommunicationTests(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=True,
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SECRET_KEY="test-secret-key-at-least-32-characters-long",
        )
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def test_vercel_json_exists_and_is_valid(self):
        """Validate vercel.json structure and proxy rewrite rules."""
        config_path = os.path.join(os.path.dirname(__file__), "..", "vercel.json")
        self.assertTrue(os.path.isfile(config_path), "vercel.json must exist in root")
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertEqual(data.get("version"), 2)
        rewrites = data.get("rewrites", [])
        self.assertTrue(len(rewrites) >= 1)
        # Verify catch-all rewrite rule forwards to Render
        catch_all = next((r for r in rewrites if r.get("source") == "/:path*"), None)
        self.assertIsNotNone(catch_all, "vercel.json must have a /:path* rewrite rule")
        self.assertTrue(
            catch_all["destination"].endswith("/:path*"),
            "Destination must forward :path* query and subpaths",
        )
        self.assertTrue(
            "onrender.com" in catch_all["destination"],
            "Destination must point to Render backend",
        )

    def test_templates_contain_csrf_meta_tag(self):
        """Verify rendered templates include <meta name='csrf-token'>."""
        # Test unauthenticated public pages
        for path in ["/", "/auth", "/register", "/help"]:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, f"Page {path} should load with 200")
            html = response.get_data(as_text=True)
            match = re.search(r'<meta\s+name=["\']csrf-token["\']\s+content=["\']([^"\']+)["\']', html)
            self.assertIsNotNone(match, f"Page {path} must contain a csrf-token meta tag")
            token = match.group(1)
            self.assertTrue(len(token) > 20, f"Page {path} CSRF token must be non-empty")

    def test_admin_verification_template_contains_csrf_meta_tag(self):
        """Verify admin verification template contains csrf-token meta tag."""
        admin_user = User(
            display_name="Admin Test",
            email="admin@example.test",
            is_admin=True,
            password_hash="test",
            roles_json='["student"]',
        )
        db.session.add(admin_user)
        db.session.commit()

        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(admin_user.id)
            sess["_fresh"] = True

        response = self.client.get("/admin/verification")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        match = re.search(r'<meta\s+name=["\']csrf-token["\']\s+content=["\']([^"\']+)["\']', html)
        self.assertIsNotNone(match, "admin/verification.html must contain a csrf-token meta tag")
        self.assertTrue(len(match.group(1)) > 20)

    def test_api_csrf_token_endpoint_preserved(self):
        """Verify GET /api/csrf-token endpoint returns a valid CSRF token."""
        response = self.client.get("/api/csrf-token")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertIsNotNone(data)
        self.assertIn("csrf_token", data)
        self.assertTrue(len(data["csrf_token"]) > 20)

    def test_post_without_csrf_token_is_rejected(self):
        """Verify CSRF validation is active: POST without CSRF token returns 400."""
        response = self.client.post(
            "/api/auth/login",
            json={"email": "nobody@example.test", "password": "wrong"},
        )
        self.assertEqual(response.status_code, 400)

    def test_post_with_csrf_token_from_page_is_accepted(self):
        """Verify CSRF token extracted from meta tag allows POST request to proceed."""
        # 1. Fetch the login page to establish session and get the token from the meta tag
        page_res = self.client.get("/auth")
        self.assertEqual(page_res.status_code, 200)
        html = page_res.get_data(as_text=True)
        match = re.search(r'<meta\s+name=["\']csrf-token["\']\s+content=["\']([^"\']+)["\']', html)
        self.assertIsNotNone(match)
        token = match.group(1)

        # 2. Submit POST with X-CSRFToken header using the extracted token
        post_res = self.client.post(
            "/api/auth/login",
            json={"email": "nobody@example.test", "password": "wrong"},
            headers={"X-CSRFToken": token},
        )
        # Should not fail CSRF (400), but fail with credential error (401 or 400 with invalid email)
        self.assertNotEqual(post_res.status_code, 400)
        data = post_res.get_json()
        self.assertFalse(data.get("ok", True))
        self.assertEqual(post_res.status_code, 401)


if __name__ == "__main__":
    unittest.main()
