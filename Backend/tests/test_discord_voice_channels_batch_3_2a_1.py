"""Focused tests for Batch 3.2A-1: Discord private voice channel REST client.

Safety: Discord HTTP is fully mocked (``discord_oauth.urlopen`` or
``discord_oauth._bot_api_request``), so no real network, Discord, database or
Supabase access can occur. DUMMY credentials and IDs only. No sleeps happen:
``time.sleep`` is patched so any retry/backoff attempt would be detected.
"""
import http.client
import io
import json
import logging
import socket
import unittest
from email.message import Message
from unittest import mock
from urllib.error import HTTPError, URLError

try:
    from Backend import discord_oauth
except ImportError:
    import discord_oauth

GUILD_ID = "111111111111111111"
STUDENT_ID = "222222222222222222"
PROVIDER_ID = "333333333333333333"
CHANNEL_ID = "444444444444444444"
BOT_TOKEN = "DUMMY.BOT.TOKEN-never-real-0123456789"
SETTINGS = {"guild_id": GUILD_ID, "bot_token": BOT_TOKEN}
NAME = "Session-42"

EVERYONE_DENY = 1024 | 1048576
PARTICIPANT_ALLOW = 1024 | 1048576 | 2097152 | 512 | 33554432


class _FakeResponse:
    def __init__(self, status, body=b""):
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


def _overwrites(student=STUDENT_ID, provider=PROVIDER_ID):
    return [
        {"id": GUILD_ID, "type": 0, "allow": "0", "deny": str(EVERYONE_DENY)},
        {"id": student, "type": 1, "allow": str(PARTICIPANT_ALLOW), "deny": "0"},
        {"id": provider, "type": 1, "allow": str(PARTICIPANT_ALLOW), "deny": "0"},
    ]


def _channel_body(**overrides):
    body = {
        "id": CHANNEL_ID,
        "type": 2,
        "guild_id": GUILD_ID,
        "name": NAME,
        "permission_overwrites": _overwrites(),
    }
    body.update(overrides)
    return json.dumps(body).encode()


class _Base(unittest.TestCase):
    def setUp(self):
        sleeper = mock.patch("time.sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)
        self.log_stream = io.StringIO()
        handler = logging.StreamHandler(self.log_stream)
        handler.setLevel(logging.DEBUG)
        root = logging.getLogger()
        self._old_level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        self.addCleanup(root.setLevel, self._old_level)

    def create(self, response=None, exc=None, name=NAME, student=STUDENT_ID, provider=PROVIDER_ID):
        with mock.patch.object(discord_oauth, "urlopen") as fake:
            if exc is not None:
                fake.side_effect = exc
            else:
                fake.return_value = response
            result = discord_oauth.create_private_voice_channel(SETTINGS, name, student, provider)
        return result, fake

    def delete(self, response=None, exc=None, channel_id=CHANNEL_ID):
        with mock.patch.object(discord_oauth, "urlopen") as fake:
            if exc is not None:
                fake.side_effect = exc
            else:
                fake.return_value = response
            result = discord_oauth.delete_voice_channel(SETTINGS, channel_id)
        return result, fake

    def assertNoSecrets(self, *things):
        blob = " ".join(repr(t) for t in things) + self.log_stream.getvalue()
        self.assertNotIn(BOT_TOKEN, blob)
        self.assertNotIn("DUMMY.BOT", blob)

    def assertNoNetworkOrSleep(self, fake):
        fake.assert_not_called()
        self.sleep.assert_not_called()


class CreateRequestShapeTests(_Base):
    def sent(self):
        result, fake = self.create(_FakeResponse(201, _channel_body()))
        self.assertEqual(fake.call_count, 1)
        req = fake.call_args[0][0]
        return result, req, json.loads(req.data.decode())

    def test_01_endpoint_method_auth_and_timeout(self):
        _, req, _ = self.sent()
        self.assertEqual(req.full_url, "https://discord.com/api/v10/guilds/%s/channels" % GUILD_ID)
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Authorization"), "Bot " + BOT_TOKEN)
        self.assertEqual(req.get_header("Content-type"), "application/json")
        with mock.patch.object(discord_oauth, "urlopen", return_value=_FakeResponse(201, _channel_body())) as fake:
            discord_oauth.create_private_voice_channel(SETTINGS, NAME, STUDENT_ID, PROVIDER_ID)
        self.assertEqual(fake.call_args[1]["timeout"], 8)

    def test_02_voice_channel_type_and_name(self):
        _, _, body = self.sent()
        self.assertEqual(body["type"], 2)
        self.assertEqual(body["name"], NAME)
        self.assertEqual(set(body), {"name", "type", "permission_overwrites"})

    def test_03_everyone_denied_view_and_connect(self):
        _, _, body = self.sent()
        everyone = body["permission_overwrites"][0]
        self.assertEqual(everyone, {"id": GUILD_ID, "type": 0, "allow": "0", "deny": "1049600"})

    def test_04_student_explicitly_allowed(self):
        _, _, body = self.sent()
        self.assertEqual(body["permission_overwrites"][1],
                         {"id": STUDENT_ID, "type": 1, "allow": "36701696", "deny": "0"})

    def test_05_provider_explicitly_allowed(self):
        _, _, body = self.sent()
        self.assertEqual(body["permission_overwrites"][2],
                         {"id": PROVIDER_ID, "type": 1, "allow": "36701696", "deny": "0"})

    def test_06_exactly_three_overwrites_no_public_access_and_decimal_strings(self):
        _, req, body = self.sent()
        overwrites = body["permission_overwrites"]
        self.assertEqual(len(overwrites), 3)
        self.assertEqual([o["id"] for o in overwrites], [GUILD_ID, STUDENT_ID, PROVIDER_ID])
        # nothing but @everyone is a role overwrite, and @everyone never gets an allow
        self.assertEqual([o["type"] for o in overwrites], [0, 1, 1])
        self.assertEqual(overwrites[0]["allow"], "0")
        for o in overwrites:
            self.assertIsInstance(o["allow"], str)
            self.assertIsInstance(o["deny"], str)
            self.assertTrue(o["allow"].isdigit() and o["deny"].isdigit())
        self.assertIn(b'"allow":"36701696"', req.data)  # serialised as strings on the wire

    def test_06b_permission_constants(self):
        self.assertEqual(discord_oauth.PERMISSION_VIEW_CHANNEL, 1024)
        self.assertEqual(discord_oauth.PERMISSION_CONNECT, 1048576)
        self.assertEqual(discord_oauth.PERMISSION_SPEAK, 2097152)
        self.assertEqual(discord_oauth.PERMISSION_STREAM, 512)
        self.assertEqual(discord_oauth.PERMISSION_USE_VAD, 33554432)
        self.assertEqual(discord_oauth.EVERYONE_DENY, EVERYONE_DENY)
        self.assertEqual(discord_oauth.PARTICIPANT_ALLOW, PARTICIPANT_ALLOW)
        # Literal check: OR of the five spec constants. (IMPLEMENTATION_NOTES.md listed 36701184,
        # which omits STREAM=512; the correct decimal value is 36701696.)
        self.assertEqual(PARTICIPANT_ALLOW, 36701696)
        self.assertEqual(PARTICIPANT_ALLOW & 512, 512)

    def test_06c_secret_only_in_authorization_header(self):
        _, req, _ = self.sent()
        self.assertNotIn(BOT_TOKEN.encode(), req.data)
        self.assertNotIn(BOT_TOKEN, req.full_url)


class CreateValidationTests(_Base):
    def test_07_discord_id_validation(self):
        bad = ["", "123", "abc" * 6, "1" * 14, "1" * 26, STUDENT_ID + "\n", " " + STUDENT_ID,
               STUDENT_ID + "/../x", None, 222222222222222222, ["222222222222222222"], "١٢٣٤٥٦٧٨٩٠١٢٣٤٥٦"]
        for value in bad:
            with self.subTest(student=value), mock.patch.object(discord_oauth, "urlopen") as fake:
                with self.assertRaises(discord_oauth.DiscordChannelValidationError):
                    discord_oauth.create_private_voice_channel(SETTINGS, NAME, value, PROVIDER_ID)
                self.assertNoNetworkOrSleep(fake)
            with self.subTest(provider=value), mock.patch.object(discord_oauth, "urlopen") as fake:
                with self.assertRaises(discord_oauth.DiscordChannelValidationError):
                    discord_oauth.create_private_voice_channel(SETTINGS, NAME, STUDENT_ID, value)
                self.assertNoNetworkOrSleep(fake)

    def test_07b_invalid_guild_or_token_settings(self):
        for settings in ({}, None, {"guild_id": "abc", "bot_token": BOT_TOKEN},
                         {"guild_id": GUILD_ID, "bot_token": ""},
                         {"guild_id": GUILD_ID, "bot_token": "bad token with space"},
                         {"guild_id": GUILD_ID, "bot_token": BOT_TOKEN + "\r\nX-Evil: 1"},
                         {"guild_id": GUILD_ID + "\n", "bot_token": BOT_TOKEN}):
            with self.subTest(settings=repr(settings)[:20]), mock.patch.object(discord_oauth, "urlopen") as fake:
                with self.assertRaises(discord_oauth.DiscordConfigurationError) as ctx:
                    discord_oauth.create_private_voice_channel(settings, NAME, STUDENT_ID, PROVIDER_ID)
                self.assertNotIn(BOT_TOKEN, str(ctx.exception))
                self.assertNoNetworkOrSleep(fake)

    def test_08_identical_student_and_provider_rejected(self):
        with mock.patch.object(discord_oauth, "urlopen") as fake:
            with self.assertRaises(discord_oauth.DiscordChannelValidationError):
                discord_oauth.create_private_voice_channel(SETTINGS, NAME, STUDENT_ID, STUDENT_ID)
            self.assertNoNetworkOrSleep(fake)

    def test_08b_participant_equal_to_guild_id_rejected(self):
        for student, provider in ((GUILD_ID, PROVIDER_ID), (STUDENT_ID, GUILD_ID)):
            with mock.patch.object(discord_oauth, "urlopen") as fake:
                with self.assertRaises(discord_oauth.DiscordChannelValidationError):
                    discord_oauth.create_private_voice_channel(SETTINGS, NAME, student, provider)
                self.assertNoNetworkOrSleep(fake)

    def test_09_invalid_channel_names_rejected(self):
        bad = ["", " ", "   ", "-", "---", "__", "a" * 101, "line\nbreak", "tab\tname", "trailing ", " leading",
               "emoji\U0001f600", "<script>", "a/b", "#general", "name\n", "ünïcode", "@everyone", "a.b",
               None, 42, b"Session-1", ["Session-1"]]
        for name in bad:
            with self.subTest(name=repr(name)), mock.patch.object(discord_oauth, "urlopen") as fake:
                with self.assertRaises(discord_oauth.DiscordChannelValidationError):
                    discord_oauth.create_private_voice_channel(SETTINGS, name, STUDENT_ID, PROVIDER_ID)
                self.assertNoNetworkOrSleep(fake)

    def test_09b_valid_channel_names_accepted(self):
        for name in ("Session-42", "learn2play_session 42", "a", "A" * 100, "7"):
            with self.subTest(name=name):
                self.assertEqual(discord_oauth.validate_channel_name(name), name)

    def test_09c_validation_errors_never_echo_values(self):
        marker = "SECRET-MARKER\nnotes"
        with self.assertRaises(discord_oauth.DiscordChannelValidationError) as ctx:
            discord_oauth.validate_channel_name(marker)
        self.assertNotIn("SECRET-MARKER", str(ctx.exception))
        with self.assertRaises(discord_oauth.DiscordChannelValidationError) as ctx:
            discord_oauth.build_private_voice_overwrites(GUILD_ID, "SECRET-MARKER", PROVIDER_ID)
        self.assertNotIn("SECRET-MARKER", str(ctx.exception))


class CreateResponseTests(_Base):
    def test_10_success_201_validated(self):
        result, fake = self.create(_FakeResponse(201, _channel_body()))
        self.assertEqual(result, {"ok": True, "status": "CHANNEL_CREATED", "channel_id": CHANNEL_ID,
                                  "outcome_unknown": False})
        self.assertEqual(fake.call_count, 1)

    def test_10b_overwrite_order_and_numeric_strings_tolerated(self):
        shuffled = list(reversed(_overwrites()))
        result, _ = self.create(_FakeResponse(201, _channel_body(permission_overwrites=shuffled)))
        self.assertTrue(result["ok"])

    def test_10c_201_that_does_not_validate_fails_closed(self):
        weakened = _overwrites()
        weakened[0]["deny"] = "1024"  # @everyone can still CONNECT
        permissive = _overwrites() + [{"id": "555555555555555555", "type": 1, "allow": "1024", "deny": "0"}]
        cases = {
            "wrong type": _channel_body(type=0),
            "bool type": _channel_body(type=True),
            "wrong guild": _channel_body(guild_id="999999999999999999"),
            "missing guild": _channel_body(guild_id=None),
            "missing id": _channel_body(id=None),
            "bad id": _channel_body(id="abc"),
            "numeric id": _channel_body(id=444444444444444444),
            "no overwrites": _channel_body(permission_overwrites=[]),
            "overwrites not list": _channel_body(permission_overwrites={}),
            "weakened everyone deny": _channel_body(permission_overwrites=weakened),
            "extra public overwrite": _channel_body(permission_overwrites=permissive),
            "duplicate overwrites": _channel_body(permission_overwrites=[_overwrites()[0]] * 3),
            "malformed overwrite": _channel_body(permission_overwrites=["x", "y", "z"]),
        }
        for label, body in cases.items():
            with self.subTest(label):
                result, _ = self.create(_FakeResponse(201, body))
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "OUTCOME_UNKNOWN")
                self.assertTrue(result["outcome_unknown"])
                self.assertIsNone(result["channel_id"])  # never a "success" id

    def test_10d_unverified_id_exposed_only_for_cleanup_when_syntactically_valid(self):
        result, _ = self.create(_FakeResponse(201, _channel_body(permission_overwrites=[])))
        self.assertEqual(result["unverified_channel_id"], CHANNEL_ID)
        self.assertIsNone(result["channel_id"])
        result, _ = self.create(_FakeResponse(201, _channel_body(id="abc", permission_overwrites=[])))
        self.assertNotIn("unverified_channel_id", result)

    def test_10e_other_2xx_is_not_success(self):
        for code in (200, 202, 204):
            with self.subTest(code=code):
                result, _ = self.create(_FakeResponse(code, _channel_body()))
                self.assertFalse(result["ok"])
                self.assertTrue(result["outcome_unknown"])
                self.assertIsNone(result["channel_id"])

    def test_11_http_400_fails_safely(self):
        result, fake = self.create(exc=_http_error(400, b'{"message": "Invalid Form Body", "code": 50035}'))
        self.assertEqual(result, {"ok": False, "status": "REQUEST_REJECTED", "channel_id": None,
                                  "outcome_unknown": False})
        self.assertEqual(fake.call_count, 1)
        self.assertNotIn("Invalid Form Body", repr(result))

    def test_12_http_401_403_fail_safely(self):
        for code in (401, 403):
            with self.subTest(code=code):
                result, fake = self.create(exc=_http_error(code, b'{"message": "Missing Permissions", "code": 50013}'))
                self.assertEqual(result["status"], "BOT_FORBIDDEN")
                self.assertFalse(result["ok"])
                self.assertFalse(result["outcome_unknown"])
                self.assertEqual(fake.call_count, 1)
                self.assertNotIn("Missing Permissions", repr(result))

    def test_13_http_404_is_guild_configuration_error(self):
        result, _ = self.create(exc=_http_error(404, b'{"message": "Unknown Guild", "code": 10004}'))
        self.assertEqual(result["status"], "GUILD_NOT_FOUND")
        self.assertFalse(result["ok"])
        self.assertFalse(result["outcome_unknown"])

    def test_14_http_429_no_retry_no_sleep(self):
        result, fake = self.create(exc=_http_error(429, b'{"retry_after": 2.5, "global": false}', {"Retry-After": "3"}))
        self.assertEqual(result["status"], "RATE_LIMITED")
        self.assertFalse(result["ok"])
        self.assertFalse(result["outcome_unknown"])
        self.assertEqual(result["retry_after"], 3.0)
        self.assertEqual(fake.call_count, 1)
        self.sleep.assert_not_called()

    def test_14b_429_with_hostile_retry_after_is_bounded(self):
        for header in ("nan", "inf", "-5", "99999999", "x"):
            with self.subTest(header=header):
                result, _ = self.create(exc=_http_error(429, b"{}", {"Retry-After": header}))
                self.assertEqual(result["status"], "RATE_LIMITED")
                self.assertNotIn("retry_after", result)

    def test_15_http_5xx_unavailable_and_flagged_ambiguous(self):
        for code in (500, 502, 503, 504):
            with self.subTest(code=code):
                result, fake = self.create(exc=_http_error(code, b"upstream error"))
                self.assertEqual(result["status"], "UNAVAILABLE")
                self.assertFalse(result["ok"])
                self.assertTrue(result["outcome_unknown"])  # gateway errors can follow a real create
                self.assertIsNone(result["channel_id"])
                self.assertEqual(fake.call_count, 1)

    def test_15b_other_4xx_rejected(self):
        for code in (405, 413, 422):
            with self.subTest(code=code):
                result, _ = self.create(exc=_http_error(code, b"{}"))
                self.assertEqual(result["status"], "REQUEST_REJECTED")
                self.assertFalse(result["outcome_unknown"])

    def test_16_timeout_and_network_failure_are_outcome_unknown(self):
        excs = [TimeoutError("timed out"), socket.timeout("timed out"), URLError(TimeoutError("timed out")),
                URLError("dns failure"), OSError("connection reset"), ConnectionResetError(),
                http.client.RemoteDisconnected("closed"), http.client.IncompleteRead(b"x"),
                RuntimeError("unexpected")]
        for exc in excs:
            with self.subTest(exc=type(exc).__name__):
                result, fake = self.create(exc=exc)
                self.assertEqual(result, {"ok": False, "status": "OUTCOME_UNKNOWN", "channel_id": None,
                                          "outcome_unknown": True})
                self.assertEqual(fake.call_count, 1)  # never retried
        self.sleep.assert_not_called()

    def test_17_invalid_json_or_oversized_response_fails_closed(self):
        big = b'{"id": "' + b"1" * (discord_oauth.MAX_RESPONSE_BYTES + 10) + b'"}'
        bodies = [b"not json", b"", b"[]", b'"str"', b"\xff\xfe\x00", big, b"null"]
        for body in bodies:
            with self.subTest(body=body[:12]):
                result, _ = self.create(_FakeResponse(201, body))
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "OUTCOME_UNKNOWN")
                self.assertTrue(result["outcome_unknown"])
                self.assertIsNone(result["channel_id"])

    def test_17b_response_read_is_bounded(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.status = 201
        resp.read.return_value = _channel_body()
        with mock.patch.object(discord_oauth, "urlopen", return_value=resp):
            discord_oauth.create_private_voice_channel(SETTINGS, NAME, STUDENT_ID, PROVIDER_ID)
        resp.read.assert_called_once_with(discord_oauth.MAX_RESPONSE_BYTES + 1)


class DeleteTests(_Base):
    def test_18_delete_204_success_and_request_shape(self):
        result, fake = self.delete(_FakeResponse(204))
        self.assertEqual(result, {"ok": True, "status": "CHANNEL_DELETED", "channel_id": CHANNEL_ID,
                                  "outcome_unknown": False, "already_absent": False})
        req = fake.call_args[0][0]
        self.assertEqual(req.full_url, "https://discord.com/api/v10/channels/%s" % CHANNEL_ID)
        self.assertEqual(req.get_method(), "DELETE")
        self.assertIsNone(req.data)
        self.assertFalse(req.has_header("Content-type"))
        self.assertEqual(req.get_header("Authorization"), "Bot " + BOT_TOKEN)
        self.assertEqual(fake.call_args[1]["timeout"], 8)
        self.assertEqual(fake.call_count, 1)

    def test_18b_delete_200_with_channel_object_is_success(self):
        result, _ = self.delete(_FakeResponse(200, json.dumps({"id": CHANNEL_ID, "type": 2}).encode()))
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "CHANNEL_DELETED")
        result, _ = self.delete(_FakeResponse(200, json.dumps({"id": "999999999999999999"}).encode()))
        self.assertFalse(result["ok"])

    def test_18c_delete_200_requires_matching_channel_id(self):
        for raw in (b"", b"not-json", b"{}", b'{"id": null}'):
            with self.subTest(body=raw):
                result, _ = self.delete(_FakeResponse(200, raw))
                self.assertFalse(result["ok"])

    def test_19_delete_404_is_idempotent(self):
        result, _ = self.delete(exc=_http_error(404, b'{"message": "Unknown Channel", "code": 10003}'))
        self.assertEqual(result, {"ok": True, "status": "CHANNEL_ALREADY_ABSENT", "channel_id": CHANNEL_ID,
                                  "outcome_unknown": False, "already_absent": True})

    def test_20_delete_permission_rate_limit_server_and_network_failures(self):
        for code in (401, 403):
            with self.subTest(code=code):
                result, fake = self.delete(exc=_http_error(code, b'{"code": 50013}'))
                self.assertEqual(result["status"], "BOT_FORBIDDEN")
                self.assertFalse(result["ok"])
                self.assertEqual(fake.call_count, 1)
        result, fake = self.delete(exc=_http_error(429, b'{"retry_after": 1.5}', {"Retry-After": "2"}))
        self.assertEqual((result["status"], result["retry_after"], fake.call_count), ("RATE_LIMITED", 2.0, 1))
        self.assertFalse(result["ok"])
        for code in (500, 502, 503):
            with self.subTest(code=code):
                result, fake = self.delete(exc=_http_error(code))
                self.assertEqual(result["status"], "UNAVAILABLE")
                self.assertFalse(result["ok"])
                self.assertEqual(fake.call_count, 1)
        for exc in (TimeoutError(), URLError("down"), OSError("reset"), http.client.RemoteDisconnected("x")):
            with self.subTest(exc=type(exc).__name__):
                result, fake = self.delete(exc=exc)
                self.assertEqual(result["status"], "UNAVAILABLE")
                self.assertFalse(result["ok"])
                self.assertTrue(result["outcome_unknown"])
                self.assertFalse(result["already_absent"])
                self.assertEqual(fake.call_count, 1)
        self.sleep.assert_not_called()

    def test_20b_delete_other_4xx_rejected(self):
        result, _ = self.delete(exc=_http_error(400, b"{}"))
        self.assertEqual(result["status"], "REQUEST_REJECTED")
        self.assertFalse(result["ok"])

    def test_20c_delete_rejects_non_snowflakes_and_urls(self):
        bad = ["", "123", "https://discord.com/api/v10/channels/" + CHANNEL_ID, "../" + CHANNEL_ID,
               CHANNEL_ID + "\n", CHANNEL_ID + "?x=1", CHANNEL_ID + "/messages", None, 444444444444444444,
               "@me", "a" * 20]
        for value in bad:
            with self.subTest(value=repr(value)[:30]), mock.patch.object(discord_oauth, "urlopen") as fake:
                with self.assertRaises(discord_oauth.DiscordChannelValidationError):
                    discord_oauth.delete_voice_channel(SETTINGS, value)
                self.assertNoNetworkOrSleep(fake)

    def test_20d_delete_validates_bot_settings(self):
        with mock.patch.object(discord_oauth, "urlopen") as fake:
            with self.assertRaises(discord_oauth.DiscordConfigurationError):
                discord_oauth.delete_voice_channel({"guild_id": GUILD_ID, "bot_token": "bad token"}, CHANNEL_ID)
            self.assertNoNetworkOrSleep(fake)


class SecretHandlingTests(_Base):
    def test_21_secrets_never_in_results_errors_or_logs(self):
        outputs = []
        outputs.append(self.create(_FakeResponse(201, _channel_body()))[0])
        for code in (400, 401, 403, 404, 429, 500):
            body = json.dumps({"message": "echo " + BOT_TOKEN}).encode()
            outputs.append(self.create(exc=_http_error(code, body))[0])
            outputs.append(self.delete(exc=_http_error(code, body))[0])
        for exc in (TimeoutError(BOT_TOKEN), URLError(BOT_TOKEN), OSError(BOT_TOKEN), RuntimeError(BOT_TOKEN)):
            outputs.append(self.create(exc=exc)[0])
            outputs.append(self.delete(exc=exc)[0])
        outputs.append(self.create(_FakeResponse(201, BOT_TOKEN.encode()))[0])
        errors = []
        for call in (lambda: discord_oauth.create_private_voice_channel(SETTINGS, "bad\n", STUDENT_ID, PROVIDER_ID),
                     lambda: discord_oauth.delete_voice_channel(SETTINGS, "x"),
                     lambda: discord_oauth.create_private_voice_channel({"guild_id": GUILD_ID, "bot_token": BOT_TOKEN + " "},
                                                                        NAME, STUDENT_ID, PROVIDER_ID)):
            with self.assertRaises(Exception) as ctx:
                call()
            errors.append(ctx.exception)
        self.assertNoSecrets(outputs, errors, [str(e) for e in errors])

    def test_21b_results_contain_only_whitelisted_keys(self):
        allowed = {"ok", "status", "channel_id", "outcome_unknown", "already_absent", "retry_after",
                   "unverified_channel_id"}
        for result in (self.create(exc=_http_error(429, b"{}", {"Retry-After": "1"}))[0],
                       self.create(_FakeResponse(201, _channel_body()))[0],
                       self.delete(_FakeResponse(204))[0], self.delete(exc=_http_error(404))[0]):
            self.assertLessEqual(set(result), allowed)


class TransportAndRegressionTests(_Base):
    def test_22_bot_api_request_rejects_unknown_methods(self):
        with mock.patch.object(discord_oauth, "urlopen") as fake:
            for method in ("PUT", "PATCH", "get", ""):
                with self.assertRaises(ValueError):
                    discord_oauth._bot_api_request(method, "https://discord.com/api/v10/x", BOT_TOKEN)
            fake.assert_not_called()

    def test_22a_bot_auth_header_not_forwarded_on_redirect(self):
        # urllib's redirect handler only copies req.headers, not
        # req.unredirected_hdrs. Bot credentials must stay on the original request.
        from urllib.request import HTTPRedirectHandler

        with mock.patch.object(discord_oauth, "urlopen", return_value=_FakeResponse(200, b"{}")) as fake:
            discord_oauth._bot_http_request("https://discord.com/api/v10/x", BOT_TOKEN)
        req = fake.call_args[0][0]
        self.assertEqual(req.get_header("Authorization"), "Bot " + BOT_TOKEN)
        redirected = HTTPRedirectHandler().redirect_request(
            req, None, 302, "Found", {}, "https://untrusted.example/steal"
        )
        self.assertIsNotNone(redirected)
        self.assertIsNone(redirected.get_header("Authorization"))

    def test_22b_bot_http_request_contract_unchanged(self):
        member = json.dumps({"user": {"id": STUDENT_ID}, "roles": []}).encode()
        with mock.patch.object(discord_oauth, "urlopen", return_value=_FakeResponse(200, member)) as fake:
            out = discord_oauth._bot_http_request("https://discord.com/api/v10/guilds/1/members/2", BOT_TOKEN)
        req = fake.call_args[0][0]
        self.assertEqual((req.get_method(), req.data), ("GET", None))
        self.assertFalse(req.has_header("Content-type"))
        self.assertEqual(req.get_header("Authorization"), "Bot " + BOT_TOKEN)
        self.assertEqual(out, {"status": 200, "payload": {"user": {"id": STUDENT_ID}, "roles": []}, "retry_after": None})
        with mock.patch.object(discord_oauth, "urlopen", side_effect=_http_error(429, b'{"retry_after": 2}', {"Retry-After": "3"})):
            out = discord_oauth._bot_http_request("https://discord.com/api/v10/x", BOT_TOKEN)
        self.assertEqual(out, {"status": 429, "payload": {"retry_after": 2}, "retry_after": 3.0})
        with mock.patch.object(discord_oauth, "urlopen", side_effect=URLError("down")):
            with self.assertRaises(discord_oauth.DiscordOAuthError):
                discord_oauth._bot_http_request("https://discord.com/api/v10/x", BOT_TOKEN)

    def test_22c_check_guild_membership_unchanged(self):
        member = json.dumps({"user": {"id": STUDENT_ID}, "roles": []}).encode()
        with mock.patch.object(discord_oauth, "urlopen", return_value=_FakeResponse(200, member)) as fake:
            self.assertEqual(discord_oauth.check_guild_membership(SETTINGS, STUDENT_ID),
                             {"status": "VERIFIED_MEMBER", "is_member": True})
        self.assertEqual(fake.call_args[0][0].full_url,
                         "https://discord.com/api/v10/guilds/%s/members/%s" % (GUILD_ID, STUDENT_ID))
        with mock.patch.object(discord_oauth, "urlopen", side_effect=_http_error(404, b'{"code": 10007}')):
            self.assertEqual(discord_oauth.check_guild_membership(SETTINGS, STUDENT_ID),
                             {"status": "NOT_IN_GUILD", "is_member": False})
        with mock.patch.object(discord_oauth, "urlopen", side_effect=_http_error(403, b"{}")):
            self.assertEqual(discord_oauth.check_guild_membership(SETTINGS, STUDENT_ID)["status"], "BOT_CONFIG_ERROR")
        with mock.patch.object(discord_oauth, "urlopen", side_effect=_http_error(429, b"{}", {"Retry-After": "4"})):
            self.assertEqual(discord_oauth.check_guild_membership(SETTINGS, STUDENT_ID),
                             {"status": "CHECK_UNAVAILABLE", "is_member": None, "retry_after": 4.0})
        with mock.patch.object(discord_oauth, "urlopen", side_effect=TimeoutError()):
            self.assertEqual(discord_oauth.check_guild_membership(SETTINGS, STUDENT_ID)["status"], "CHECK_UNAVAILABLE")

    def test_22d_check_guild_membership_still_routes_through_bot_http_request(self):
        with mock.patch.object(discord_oauth, "_bot_http_request",
                               return_value={"status": 404, "payload": {"code": 10007}, "retry_after": None}) as fake:
            result = discord_oauth.check_guild_membership(SETTINGS, STUDENT_ID)
        fake.assert_called_once()
        self.assertEqual(result["status"], "NOT_IN_GUILD")

    def test_22e_oauth_helpers_unchanged(self):
        with mock.patch.object(discord_oauth, "urlopen",
                               return_value=_FakeResponse(200, b'{"access_token": "tok", "token_type": "Bearer"}')) as fake:
            token = discord_oauth.exchange_code_for_token(
                {"client_id": "c", "client_secret": "s", "redirect_uri": "https://x.test/cb"}, "code123")
        self.assertEqual(token, "tok")
        req = fake.call_args[0][0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Content-type"), "application/x-www-form-urlencoded")
        self.assertIn(b"grant_type=authorization_code", req.data)
        with mock.patch.object(discord_oauth, "urlopen", return_value=_FakeResponse(200, b'{"id": "123456789012345678"}')) as fake:
            discord_oauth._http_request(discord_oauth.DISCORD_ME_URL, headers={"Authorization": "Bearer t"})
        self.assertEqual(fake.call_args[0][0].get_method(), "GET")
        url = discord_oauth.build_authorize_url(
            {"client_id": "c", "client_secret": "s", "redirect_uri": "https://x.test/cb"}, "st")
        self.assertIn("scope=identify", url)
        self.assertEqual(discord_oauth.validate_identity({"id": STUDENT_ID}), STUDENT_ID)
        self.assertEqual(discord_oauth.get_bot_settings({"DISCORD_GUILD_ID": GUILD_ID, "DISCORD_BOT_TOKEN": BOT_TOKEN}), SETTINGS)
        with self.assertRaises(discord_oauth.DiscordConfigurationError):
            discord_oauth.get_bot_settings({"DISCORD_GUILD_ID": "", "DISCORD_BOT_TOKEN": ""})


if __name__ == "__main__":
    unittest.main()
