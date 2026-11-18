"""Focused tests for Batch 3.1B-2A: Discord guild membership verification.

Safety: every app uses an in-memory SQLite database; Discord HTTP is fully
mocked (``discord_oauth._bot_http_request`` or ``discord_oauth.urlopen``), so no
real network, Discord or Supabase call can occur. DUMMY credentials only.
"""
import io
import json
import time
import unittest
from email.message import Message
from unittest import mock
from urllib.error import HTTPError

try:
    from Backend import create_app, discord_oauth
    from Backend.extensions import db
    from Backend.models import User, utcnow
except ImportError:
    from __init__ import create_app
    import discord_oauth
    from extensions import db
    from models import User, utcnow

GUILD_ID = "111111111111111111"
USER_A_DISCORD = "123456789012345678"
USER_B_DISCORD = "987654321098765432"
BOT_TOKEN = "DUMMY.BOT.TOKEN-never-real-0123456789"


class _FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self, n=-1):
        return self._body if n is None or n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code, body=b"", headers=None):
    msg = Message()
    for key, value in (headers or {}).items():
        msg[key] = value
    return HTTPError("https://discord.com/api/v10/x", code, "err", msg, io.BytesIO(body))


def _member_body(user_id=USER_A_DISCORD):
    return json.dumps({"user": {"id": user_id, "username": "x"}, "roles": [], "joined_at": "2024-01-01T00:00:00+00:00"}).encode()


class GuildMembershipTests(unittest.TestCase):
    def make_app(self, configured=True):
        cfg = {
            "TESTING": True,
            "WTF_CSRF_ENABLED": False,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "SECRET_KEY": "test-key-3-1b-2a",
            "DISCORD_GUILD_ID": "",
            "DISCORD_BOT_TOKEN": "",
        }
        if configured:
            cfg.update(DISCORD_GUILD_ID=GUILD_ID, DISCORD_BOT_TOKEN=BOT_TOKEN)
        return create_app(cfg)

    def setUp(self):
        self.app = self.make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.a = User(display_name="A", email="a@example.test", password_hash="x", roles_json='["student"]',
                      discord_user_id=USER_A_DISCORD, discord_username="alpha", discord_linked_at=utcnow())
        self.b = User(display_name="B", email="b@example.test", password_hash="x", roles_json='["student"]',
                      discord_user_id=USER_B_DISCORD, discord_username="beta", discord_linked_at=utcnow())
        self.unlinked = User(display_name="C", email="c@example.test", password_hash="x", roles_json='["student"]')
        db.session.add_all([self.a, self.b, self.unlinked])
        db.session.commit()
        self.client = self.app.test_client()
        self.login(self.client, self.a)

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def login(self, client, user):
        from flask import g
        g.__dict__.pop("_login_user", None)
        with client.session_transaction() as s:
            s["_user_id"] = str(user.id)
            s["_fresh"] = True

    def call(self, urlopen_result=None, urlopen_exc=None, query=""):
        """GET the endpoint with Discord's urlopen mocked; returns (response, mock)."""
        with mock.patch.object(discord_oauth, "urlopen") as fake, \
                mock.patch("time.sleep") as sleeper:
            if urlopen_exc is not None:
                fake.side_effect = urlopen_exc
            else:
                fake.return_value = urlopen_result
            res = self.client.get("/api/me/discord/guild-status" + query)
            self.assertFalse(sleeper.called, "no sleeping/retry loops allowed")
        return res, fake

    def assert_state(self, res, status, is_member):
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["status"], status)
        self.assertIs(body["is_member"], is_member)
        self.assertIsInstance(body["message"], str)
        self.assertEqual(set(body), {"ok", "status", "is_member", "message"})

    # 1
    def test_01_not_linked_makes_no_outbound_call(self):
        self.login(self.client, self.unlinked)
        res, fake = self.call(_FakeResponse(200, _member_body()))
        self.assert_state(res, "NOT_LINKED", False)
        fake.assert_not_called()

    # 2
    def test_02_200_confirms_membership_and_uses_bot_auth_and_own_id(self):
        res, fake = self.call(_FakeResponse(200, _member_body()))
        self.assert_state(res, "VERIFIED_MEMBER", True)
        req = fake.call_args[0][0]
        self.assertEqual(req.full_url, "https://discord.com/api/v10/guilds/%s/members/%s" % (GUILD_ID, USER_A_DISCORD))
        self.assertEqual(req.get_header("Authorization"), "Bot " + BOT_TOKEN)
        self.assertEqual(fake.call_args[1]["timeout"], discord_oauth.NETWORK_TIMEOUT_SECONDS)

    # 3
    def test_03_404_unknown_member_means_not_in_guild(self):
        res, _ = self.call(urlopen_exc=_http_error(404, b'{"code": 10007, "message": "Unknown Member"}'))
        self.assert_state(res, "NOT_IN_GUILD", False)

    # 4
    def test_04_other_404_is_not_treated_as_non_member(self):
        for body in (b'{"code": 10013, "message": "Unknown User"}', b"not json", b"{}"):
            res, _ = self.call(urlopen_exc=_http_error(404, body))
            self.assert_state(res, "CHECK_UNAVAILABLE", None)
        res, _ = self.call(urlopen_exc=_http_error(404, b'{"code": 10004, "message": "Unknown Guild"}'))
        self.assert_state(res, "BOT_CONFIG_ERROR", None)

    # 5
    def test_05_401_is_bot_config_error(self):
        res, _ = self.call(urlopen_exc=_http_error(401, b'{"message": "401: Unauthorized", "code": 0}'))
        self.assert_state(res, "BOT_CONFIG_ERROR", None)

    # 6
    def test_06_403_is_bot_config_error(self):
        res, _ = self.call(urlopen_exc=_http_error(403, b'{"message": "Missing Access", "code": 50001}'))
        self.assert_state(res, "BOT_CONFIG_ERROR", None)

    # 7
    def test_07_429_is_unavailable_without_retry_or_sleep(self):
        res, fake = self.call(urlopen_exc=_http_error(429, b'{"retry_after": 2.5, "global": false}', {"Retry-After": "3"}))
        self.assert_state(res, "CHECK_UNAVAILABLE", None)
        self.assertEqual(fake.call_count, 1)
        self.assertNotIn("retry", res.get_data(as_text=True).lower())
        self.assertNotIn("Retry-After", res.headers)

    def test_07b_retry_after_parsing_is_safe_and_bounded(self):
        parse = discord_oauth._parse_retry_after
        self.assertEqual(parse("3", None), 3.0)
        self.assertEqual(parse(None, {"retry_after": 1.5}), 1.5)
        for bad in ("abc", "nan", "inf", "-1", "999999", "", None):
            self.assertIsNone(parse(bad, None))
        self.assertIsNone(parse(None, {"retry_after": True}))
        self.assertIsNone(parse(None, {"retry_after": [1]}))
        settings = discord_oauth.get_bot_settings({"DISCORD_GUILD_ID": GUILD_ID, "DISCORD_BOT_TOKEN": BOT_TOKEN})
        with mock.patch.object(discord_oauth, "urlopen", side_effect=_http_error(429, b"{}", {"Retry-After": "7"})):
            result = discord_oauth.check_guild_membership(settings, USER_A_DISCORD)
        self.assertEqual(result, {"status": "CHECK_UNAVAILABLE", "is_member": None, "retry_after": 7.0})

    # 8
    def test_08_5xx_and_unexpected_statuses_are_unavailable(self):
        for code in (500, 502, 503, 504, 400, 418, 302):
            res, _ = self.call(urlopen_exc=_http_error(code, b"{}"))
            self.assert_state(res, "CHECK_UNAVAILABLE", None)
        res, _ = self.call(_FakeResponse(204, b""))  # unexpected success status
        self.assert_state(res, "CHECK_UNAVAILABLE", None)

    # 9
    def test_09_timeout_and_connection_errors_are_unavailable(self):
        for exc in (TimeoutError("t"), ConnectionResetError("c"), OSError("o"),
                    __import__("urllib.error").error.URLError("dns"),
                    __import__("http.client").client.RemoteDisconnected("r")):
            res, _ = self.call(urlopen_exc=exc)
            self.assert_state(res, "CHECK_UNAVAILABLE", None)

    # 10
    def test_10_malformed_responses_are_unavailable(self):
        bodies = [
            b"<html>oops</html>", b"[]", b"{}", b'{"user": null}',
            json.dumps({"user": {"id": USER_B_DISCORD}, "roles": []}).encode(),   # wrong user
            json.dumps({"user": {"id": USER_A_DISCORD}, "roles": "admin"}).encode(),
            b"x" * (discord_oauth.MAX_RESPONSE_BYTES + 10),                        # oversized
        ]
        for body in bodies:
            res, _ = self.call(_FakeResponse(200, body))
            self.assert_state(res, "CHECK_UNAVAILABLE", None)

    # 11
    def test_11_missing_or_bad_bot_credentials_is_bot_config_error(self):
        for guild, token in (("", ""), (GUILD_ID, ""), ("", BOT_TOKEN), ("not-a-snowflake", BOT_TOKEN),
                             (GUILD_ID, "bad token with spaces"), (GUILD_ID, "tok\r\nInjected: 1")):
            self.app.config["DISCORD_GUILD_ID"] = guild
            self.app.config["DISCORD_BOT_TOKEN"] = token
            res, fake = self.call(_FakeResponse(200, _member_body()))
            self.assert_state(res, "BOT_CONFIG_ERROR", None)
            fake.assert_not_called()
        with self.assertRaises(discord_oauth.DiscordConfigurationError) as ctx:
            discord_oauth.get_bot_settings({"DISCORD_GUILD_ID": GUILD_ID, "DISCORD_BOT_TOKEN": ""})
        self.assertNotIn(GUILD_ID, str(ctx.exception))

    def test_11b_app_starts_without_bot_settings(self):
        app = self.make_app(configured=False)  # must not raise
        self.assertEqual(app.config["DISCORD_BOT_TOKEN"], "")

    # 12
    def test_12_anonymous_cannot_access(self):
        from flask import g
        g.__dict__.pop("_login_user", None)
        anon = self.app.test_client()
        with mock.patch.object(discord_oauth, "urlopen") as fake:
            res = anon.get("/api/me/discord/guild-status")
        self.assertIn(res.status_code, (301, 302, 401))
        fake.assert_not_called()

    def test_12b_inactive_user_blocked(self):
        self.a.is_active = False
        db.session.commit()
        res, fake = self.call(_FakeResponse(200, _member_body()))
        # The user loader may reject inactive users first (redirect) or the route returns 403.
        self.assertIn(res.status_code, (302, 401, 403))
        fake.assert_not_called()

    # 13
    def test_13_browser_supplied_ids_cannot_change_checked_identity(self):
        qs = "?discord_user_id=%s&user_id=%s&guild_id=999999999999999999&id=%s" % (USER_B_DISCORD, USER_B_DISCORD, USER_B_DISCORD)
        with mock.patch.object(discord_oauth, "urlopen", return_value=_FakeResponse(200, _member_body())) as fake:
            res = self.client.get("/api/me/discord/guild-status" + qs, headers={"X-Discord-User-Id": USER_B_DISCORD})
            res2 = self.client.open("/api/me/discord/guild-status", method="GET",
                                    json={"discord_user_id": USER_B_DISCORD, "guild_id": "999999999999999999"})
        self.assertEqual(res.get_json()["status"], "VERIFIED_MEMBER")
        self.assertEqual(res2.get_json()["status"], "VERIFIED_MEMBER")
        for call in fake.call_args_list:
            url = call[0][0].full_url
            self.assertIn("/guilds/%s/members/%s" % (GUILD_ID, USER_A_DISCORD), url)
            self.assertNotIn(USER_B_DISCORD, url)
            self.assertNotIn("999999999999999999", url)

    def test_13b_invalid_stored_or_passed_id_never_reaches_network(self):
        settings = discord_oauth.get_bot_settings({"DISCORD_GUILD_ID": GUILD_ID, "DISCORD_BOT_TOKEN": BOT_TOKEN})
        with mock.patch.object(discord_oauth, "urlopen") as fake:
            for bad in ("../../x", "12", "123456789012345678/roles", 123456789012345678, None, "1234567890123456a8"):
                result = discord_oauth.check_guild_membership(settings, bad)
                self.assertEqual(result, {"status": "CHECK_UNAVAILABLE", "is_member": None})
        fake.assert_not_called()

    # 14
    def test_14_cache_control_no_store(self):
        self.login(self.client, self.unlinked)
        cases = [(None, _FakeResponse(200, _member_body())), (TimeoutError("t"), None)]
        res, _ = self.call(_FakeResponse(200, _member_body()))
        self.assertIn("no-store", res.headers["Cache-Control"])
        res, _ = self.call(urlopen_exc=TimeoutError("t"))
        self.assertIn("no-store", res.headers["Cache-Control"])
        self.assertEqual(res.mimetype, "application/json")
        res, _ = self.call(urlopen_exc=_http_error(404, b'{"code": 10007}'))
        self.assertIn("no-store", res.headers["Cache-Control"])

    # 15
    def test_15_response_never_exposes_token_or_ids(self):
        scenarios = [
            dict(urlopen_result=_FakeResponse(200, _member_body())),
            dict(urlopen_exc=_http_error(401, ('{"message": "bad %s"}' % BOT_TOKEN).encode())),
            dict(urlopen_exc=_http_error(429, b"{}", {"Retry-After": "1", "X-RateLimit-Bucket": "secret-bucket"})),
            dict(urlopen_exc=TimeoutError(BOT_TOKEN)),
            dict(urlopen_exc=_http_error(404, b'{"code": 10007}')),
        ]
        for scenario in scenarios:
            with self.assertLogs(self.app.logger, level="DEBUG") as logs:
                self.app.logger.warning("sentinel")  # ensures assertLogs has a record
                res, _ = self.call(**scenario)
            blob = res.get_data(as_text=True) + str(dict(res.headers)) + "\n".join(logs.output)
            self.assertNotIn(BOT_TOKEN, blob)
            self.assertNotIn("DUMMY.BOT", blob)
            self.assertNotIn("secret-bucket", blob)
            self.assertNotIn(GUILD_ID, res.get_data(as_text=True))
            self.assertNotIn(USER_A_DISCORD, "\n".join(logs.output))
            self.assertNotIn("Traceback", blob)
        self.app.config["DISCORD_BOT_TOKEN"] = ""
        res, _ = self.call(_FakeResponse(200, _member_body()))
        self.assertNotIn("DISCORD_BOT_TOKEN", res.get_data(as_text=True))

    def test_16_helper_never_raises_and_status_mapping_is_exclusive(self):
        settings = {"guild_id": GUILD_ID, "bot_token": BOT_TOKEN}
        with mock.patch.object(discord_oauth, "_bot_http_request", side_effect=discord_oauth.DiscordOAuthError("x")):
            self.assertEqual(discord_oauth.check_guild_membership(settings, USER_A_DISCORD),
                             {"status": "CHECK_UNAVAILABLE", "is_member": None})
        with mock.patch.object(discord_oauth, "_bot_http_request", return_value={"status": 200, "payload": json.loads(_member_body()), "retry_after": None}):
            self.assertEqual(discord_oauth.check_guild_membership(settings, USER_A_DISCORD),
                             {"status": "VERIFIED_MEMBER", "is_member": True})

    def test_17_unexpected_helper_exception_fails_closed(self):
        with mock.patch.object(discord_oauth, "check_guild_membership", side_effect=RuntimeError(BOT_TOKEN)):
            res = self.client.get("/api/me/discord/guild-status")
        self.assert_state(res, "CHECK_UNAVAILABLE", None)
        self.assertNotIn(BOT_TOKEN, res.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
