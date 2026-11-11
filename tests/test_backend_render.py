import unittest

from wsgi import app
from werkzeug.middleware.proxy_fix import ProxyFix


class BackendRenderTests(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
        )
        self.client = self.app.test_client()

    def test_healthz_endpoint_returns_ok(self):
        """Verify /healthz returns 200 OK and expected JSON payload."""
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Content-Type"), "application/json")
        data = response.get_json()
        self.assertIsNotNone(data)
        self.assertEqual(data.get("status"), "ok")

    def test_wsgi_app_wrapped_with_proxyfix(self):
        """Verify wsgi app is wrapped with Werkzeug ProxyFix middleware."""
        self.assertIsInstance(self.app.wsgi_app, ProxyFix)

    def test_proxyfix_resolves_forwarded_host_and_proto(self):
        """Verify ProxyFix extracts X-Forwarded-Host and X-Forwarded-Proto in WSGI environ."""
        environ = {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": "/healthz",
            "SERVER_NAME": "localhost",
            "SERVER_PORT": "80",
            "wsgi.url_scheme": "http",
            "HTTP_X_FORWARDED_HOST": "learn2play.vercel.app",
            "HTTP_X_FORWARDED_PROTO": "https",
        }
        self.app.wsgi_app(environ, lambda status, headers: None)
        self.assertEqual(environ.get("HTTP_HOST"), "learn2play.vercel.app")
        self.assertEqual(environ.get("wsgi.url_scheme"), "https")


if __name__ == "__main__":
    unittest.main()
