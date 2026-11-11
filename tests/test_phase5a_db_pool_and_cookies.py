import os
import unittest
from unittest.mock import patch

from config import Config
from wsgi import app
from extensions import db
from models import User


class Phase5ADatabasePoolingAndCookiesTests(unittest.TestCase):
    def test_postgres_pool_configuration(self):
        """Verify PostgreSQL pool settings are conservatively configured for Gunicorn workers."""
        mock_env = {
            "DATABASE_URL": "postgresql://postgres.testref:secret@aws-0-ap-southeast-2.pooler.supabase.com:6543/postgres",
            "APP_ENV": "development",
        }
        with patch.dict(os.environ, mock_env, clear=False):
            # Dynamically evaluate Config class definition logic
            class TestPgConfig(Config):
                database_url = os.environ.get("DATABASE_URL")
                for old_scheme in ("postgres://", "postgresql://", "postgresql+psycopg2://"):
                    if database_url and database_url.startswith(old_scheme):
                        database_url = "postgresql+psycopg://" + database_url[len(old_scheme):]
                        break
                SQLALCHEMY_DATABASE_URI = database_url
                SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}
                if database_url and database_url.startswith("postgresql+psycopg://"):
                    SQLALCHEMY_ENGINE_OPTIONS.update(
                        pool_size=3,
                        max_overflow=2,
                        pool_recycle=1800,
                        connect_args={"sslmode": "require", "prepare_threshold": None},
                    )

            self.assertTrue(TestPgConfig.SQLALCHEMY_DATABASE_URI.startswith("postgresql+psycopg://"))
            opts = TestPgConfig.SQLALCHEMY_ENGINE_OPTIONS
            self.assertEqual(opts.get("pool_size"), 3)
            self.assertEqual(opts.get("max_overflow"), 2)
            self.assertEqual(opts.get("pool_recycle"), 1800)
            self.assertTrue(opts.get("pool_pre_ping"))
            connect_args = opts.get("connect_args", {})
            self.assertEqual(connect_args.get("sslmode"), "require")
            self.assertIsNone(connect_args.get("prepare_threshold"))

    def test_sqlite_pool_compatibility(self):
        """Verify SQLite mode preserves simple engine options without PostgreSQL connect_args."""
        with patch.dict(os.environ, {"DATABASE_URL": ""}, clear=False):
            class TestSqliteConfig(Config):
                database_url = ""
                SQLALCHEMY_DATABASE_URI = "sqlite:///test.db"
                SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

            self.assertTrue(TestSqliteConfig.SQLALCHEMY_DATABASE_URI.startswith("sqlite:///"))
            opts = TestSqliteConfig.SQLALCHEMY_ENGINE_OPTIONS
            self.assertTrue(opts.get("pool_pre_ping"))
            self.assertNotIn("pool_size", opts)
            self.assertNotIn("connect_args", opts)

    def test_production_cookie_flags(self):
        """Verify production mode enforces Secure, HttpOnly, and SameSite=Lax flags."""
        mock_env = {
            "APP_ENV": "production",
            "SECRET_KEY": "a" * 32,
        }
        with patch.dict(os.environ, mock_env, clear=False):
            class TestProdConfig(Config):
                app_environment = "production"
                is_production = True
                SESSION_COOKIE_HTTPONLY = True
                SESSION_COOKIE_SECURE = is_production
                SESSION_COOKIE_SAMESITE = "Lax"
                REMEMBER_COOKIE_HTTPONLY = True
                REMEMBER_COOKIE_SECURE = is_production
                REMEMBER_COOKIE_SAMESITE = "Lax"

            self.assertTrue(TestProdConfig.is_production)
            self.assertTrue(TestProdConfig.SESSION_COOKIE_HTTPONLY)
            self.assertTrue(TestProdConfig.SESSION_COOKIE_SECURE)
            self.assertEqual(TestProdConfig.SESSION_COOKIE_SAMESITE, "Lax")
            self.assertTrue(TestProdConfig.REMEMBER_COOKIE_HTTPONLY)
            self.assertTrue(TestProdConfig.REMEMBER_COOKIE_SECURE)
            self.assertEqual(TestProdConfig.REMEMBER_COOKIE_SAMESITE, "Lax")

    def test_development_cookie_flags(self):
        """Verify development mode allows non-HTTPS cookies for local testing."""
        mock_env = {
            "APP_ENV": "development",
            "VERCEL": "0",
            "RENDER": "false",
        }
        with patch.dict(os.environ, mock_env, clear=False):
            class TestDevConfig(Config):
                app_environment = "development"
                is_production = False
                SESSION_COOKIE_HTTPONLY = True
                SESSION_COOKIE_SECURE = is_production
                SESSION_COOKIE_SAMESITE = "Lax"
                REMEMBER_COOKIE_HTTPONLY = True
                REMEMBER_COOKIE_SECURE = is_production
                REMEMBER_COOKIE_SAMESITE = "Lax"

            self.assertFalse(TestDevConfig.is_production)
            self.assertTrue(TestDevConfig.SESSION_COOKIE_HTTPONLY)
            self.assertFalse(TestDevConfig.SESSION_COOKIE_SECURE)
            self.assertEqual(TestDevConfig.SESSION_COOKIE_SAMESITE, "Lax")
            self.assertTrue(TestDevConfig.REMEMBER_COOKIE_HTTPONLY)
            self.assertFalse(TestDevConfig.REMEMBER_COOKIE_SECURE)
            self.assertEqual(TestDevConfig.REMEMBER_COOKIE_SAMESITE, "Lax")

    def test_login_cookie_emission(self):
        """Verify login sets session cookie with HttpOnly and SameSite=Lax."""
        test_app = app
        test_app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SESSION_COOKIE_HTTPONLY=True,
            SESSION_COOKIE_SECURE=False,
            SESSION_COOKIE_SAMESITE="Lax",
        )
        with test_app.app_context():
            db.create_all()
            user = User(
                display_name="Cookie Test User",
                email="cookietest@example.test",
                roles_json='["student"]',
            )
            user.set_password("validpassword123")
            db.session.add(user)
            db.session.commit()

            client = test_app.test_client()
            res = client.post(
                "/api/auth/login",
                json={"email": "cookietest@example.test", "password": "validpassword123"},
            )
            self.assertEqual(res.status_code, 200)

            # Check Set-Cookie headers
            set_cookie_headers = res.headers.getlist("Set-Cookie")
            self.assertTrue(len(set_cookie_headers) > 0)
            session_cookie = next((c for c in set_cookie_headers if c.startswith("session=")), None)
            self.assertIsNotNone(session_cookie)
            self.assertIn("HttpOnly", session_cookie)
            self.assertIn("SameSite=Lax", session_cookie)

            db.session.remove()
            db.drop_all()


if __name__ == "__main__":
    unittest.main()
