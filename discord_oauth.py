"""Discord OAuth2 helpers (Batch 3.1A).

Everything that touches the Discord network lives in this module so tests can
mock ``fetch_identity`` (or ``_http_request``) without real network access.

Security rules enforced here:
- only the ``identify`` scope is ever requested;
- the redirect URI is always the configured value, never user input;
- network calls use bounded timeouts and bounded response sizes;
- authorization codes, tokens and the client secret are never logged and
  never placed into exception messages;
- OAuth tokens are discarded after the identity lookup and are never stored.
"""

import http.client
import json
import math
import re
import secrets
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

DISCORD_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/v10/oauth2/token"
DISCORD_ME_URL = "https://discord.com/api/v10/users/@me"
DISCORD_GUILD_MEMBER_URL = "https://discord.com/api/v10/guilds/{guild_id}/members/{user_id}"
DISCORD_SCOPE = "identify"  # the ONLY scope this application requests

STATE_TTL_SECONDS = 600
SESSION_KEY = "discord_oauth"
NETWORK_TIMEOUT_SECONDS = 8
MAX_RESPONSE_BYTES = 64 * 1024
MAX_CODE_LENGTH = 512
MAX_STATE_LENGTH = 128

class NoDiscordRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


urlopen = build_opener(NoDiscordRedirects()).open

_USER_AGENT = "DiscordBot (https://github.com/sjvibar-ops/Learn2Play, 1.0)"
_SNOWFLAKE_RE = re.compile(r"^[0-9]{15,25}$")
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


class DiscordConfigurationError(Exception):
    """Required Discord OAuth configuration is missing or unusable.

    The message only ever names setting names, never values.
    """


class DiscordOAuthError(Exception):
    """Discord rejected a request or returned an unusable response.

    Messages are generic on purpose: they never contain codes, tokens,
    secrets or raw provider responses.
    """


def get_oauth_settings(config):
    """Validate and return the OAuth settings from a mapping (e.g. app.config).

    Raises DiscordConfigurationError if anything essential is missing. Called
    only when the authorization flow starts, never at application startup.
    """
    client_id = str(config.get("DISCORD_CLIENT_ID") or "").strip()
    client_secret = str(config.get("DISCORD_CLIENT_SECRET") or "").strip()
    redirect_uri = str(config.get("DISCORD_REDIRECT_URI") or "").strip()

    missing = [
        name
        for name, value in (
            ("DISCORD_CLIENT_ID", client_id),
            ("DISCORD_CLIENT_SECRET", client_secret),
            ("DISCORD_REDIRECT_URI", redirect_uri),
        )
        if not value
    ]
    if missing:
        raise DiscordConfigurationError("Missing Discord settings: " + ", ".join(missing))

    parsed = urlparse(redirect_uri)
    secure = parsed.scheme == "https"
    local_http = parsed.scheme == "http" and (parsed.hostname or "") in _LOOPBACK_HOSTS
    if not (secure or local_http) or not parsed.netloc or parsed.fragment:
        raise DiscordConfigurationError("DISCORD_REDIRECT_URI is not a valid redirect URI.")

    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    }


def new_state_record(user_id, now=None):
    """Create a fresh (state, session_record) pair bound to ``user_id``."""
    now = int(time.time() if now is None else now)
    state = secrets.token_urlsafe(32)
    return state, {
        "state": state,
        "user_id": int(user_id),
        "expires_at": now + STATE_TTL_SECONDS,
    }


def build_authorize_url(settings, state):
    """Discord authorization URL: identify scope only, configured redirect only."""
    query = urlencode(
        {
            "client_id": settings["client_id"],
            "response_type": "code",
            "redirect_uri": settings["redirect_uri"],
            "scope": DISCORD_SCOPE,
            "state": state,
        }
    )
    return DISCORD_AUTHORIZE_URL + "?" + query


def state_matches(expected, received):
    """Constant-time state comparison that tolerates missing/odd input."""
    if not isinstance(expected, str) or not isinstance(received, str):
        return False
    if not expected or not received or len(received) > MAX_STATE_LENGTH:
        return False
    return secrets.compare_digest(expected.encode("utf-8"), received.encode("utf-8"))


def _http_request(url, data=None, headers=None):
    """The single place that performs Discord HTTP. Returns parsed JSON (dict).

    Raises DiscordOAuthError with a generic message on any failure. Tests mock
    this function (or ``fetch_identity``) so no real network is used.
    """
    request_headers = {"Accept": "application/json", "User-Agent": _USER_AGENT}
    request_headers.update(headers or {})
    body = urlencode(data).encode("ascii") if data is not None else None
    if body is not None:
        request_headers["Content-Type"] = "application/x-www-form-urlencoded"
    try:
        req = Request(url, data=body, headers=request_headers, method="POST" if body is not None else "GET")
        with urlopen(req, timeout=NETWORK_TIMEOUT_SECONDS) as resp:  # noqa: S310 - fixed https URLs
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise DiscordOAuthError("Discord rejected the request (HTTP %s)." % int(exc.code)) from None
    except (URLError, TimeoutError, OSError, http.client.HTTPException):
        raise DiscordOAuthError("Could not reach Discord.") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise DiscordOAuthError("Discord response was too large.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise DiscordOAuthError("Discord returned an unreadable response.") from None
    if not isinstance(payload, dict):
        raise DiscordOAuthError("Discord returned an unexpected response.")
    return payload


def exchange_code_for_token(settings, code):
    """Server-side authorization-code exchange. Returns the access token string."""
    payload = _http_request(
        DISCORD_TOKEN_URL,
        data={
            "client_id": settings["client_id"],
            "client_secret": settings["client_secret"],
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings["redirect_uri"],
        },
    )
    token = payload.get("access_token")
    if not isinstance(token, str) or not token or str(payload.get("token_type", "")).lower() != "bearer":
        raise DiscordOAuthError("Discord returned an invalid token response.")
    return token


def validate_identity(payload):
    """Return the validated Discord user id string, or raise DiscordOAuthError."""
    discord_id = payload.get("id") if isinstance(payload, dict) else None
    if not isinstance(discord_id, str) or not _SNOWFLAKE_RE.match(discord_id):
        raise DiscordOAuthError("Discord returned an invalid identity.")
    return discord_id


def fetch_identity(settings, code):
    """Exchange ``code`` and return the validated Discord identity.

    Returns ``{"id": <validated snowflake string>, "username": <raw str or None>}``.
    The username is raw provider text; callers must normalize it before use.

    The access token lives only inside this function and is never returned,
    logged or stored.
    """
    token = exchange_code_for_token(settings, code)
    payload = _http_request(DISCORD_ME_URL, headers={"Authorization": "Bearer " + token})
    username = payload.get("username")
    return {
        "id": validate_identity(payload),
        "username": username if isinstance(username, str) else None,
    }


# ---------------------------------------------------------------------------
# Guild membership verification (Batch 3.1B-2A)
# ---------------------------------------------------------------------------
STATUS_VERIFIED_MEMBER = "VERIFIED_MEMBER"
STATUS_NOT_IN_GUILD = "NOT_IN_GUILD"
STATUS_CHECK_UNAVAILABLE = "CHECK_UNAVAILABLE"
STATUS_BOT_CONFIG_ERROR = "BOT_CONFIG_ERROR"

DISCORD_ERROR_UNKNOWN_GUILD = 10004
DISCORD_ERROR_UNKNOWN_MEMBER = 10007
MAX_RETRY_AFTER_SECONDS = 3600
_BOT_TOKEN_RE = re.compile(r"^[\x21-\x7e]{1,256}$")  # printable ASCII, no whitespace


def get_bot_settings(config):
    """Validate and return the bot settings from a mapping (e.g. app.config).

    Raises DiscordConfigurationError (message names setting names only, never
    values). Called only when a membership check runs, never at startup.
    """
    guild_id = str(config.get("DISCORD_GUILD_ID") or "").strip()
    bot_token = str(config.get("DISCORD_BOT_TOKEN") or "").strip()

    missing = [
        name
        for name, value in (
            ("DISCORD_GUILD_ID", guild_id),
            ("DISCORD_BOT_TOKEN", bot_token),
        )
        if not value
    ]
    if missing:
        raise DiscordConfigurationError("Missing Discord bot settings: " + ", ".join(missing))
    if not _SNOWFLAKE_RE.match(guild_id):
        raise DiscordConfigurationError("DISCORD_GUILD_ID is not a valid Discord ID.")
    if not _BOT_TOKEN_RE.match(bot_token):
        raise DiscordConfigurationError("DISCORD_BOT_TOKEN is not usable.")
    return {"guild_id": guild_id, "bot_token": bot_token}


def _parse_retry_after(header_value, payload):
    """Return a bounded, finite retry delay in seconds, or None. Never raises."""
    candidates = [header_value]
    if isinstance(payload, dict):
        candidates.append(payload.get("retry_after"))
    for value in candidates:
        if value is None or isinstance(value, bool):
            continue
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(seconds) and 0 <= seconds <= MAX_RETRY_AFTER_SECONDS:
            return seconds
    return None


def _bot_http_request(url, bot_token):
    """GET ``url`` as the bot. The single place that performs bot HTTP.

    Returns ``{"status": int, "payload": dict | None, "retry_after": float | None}``
    for ANY HTTP status (the caller classifies it). Raises DiscordOAuthError
    (generic message) only when Discord cannot be reached. Tests mock this
    function or ``urlopen``; no retries or sleeps happen here.

    Thin GET wrapper over ``_bot_api_request`` (Batch 3.2A-1); contract unchanged.
    """
    return _bot_api_request("GET", url, bot_token)


def _bot_api_request(method, url, bot_token, json_body=None):
    """Perform one bot-authenticated Discord request (GET, POST JSON or DELETE).

    Same return contract and error behaviour as ``_bot_http_request``. Exactly
    one attempt is made: no retries, no sleeps. ``json_body`` (a dict) is sent as
    UTF-8 JSON and is only meaningful for POST. Tests mock this function or
    ``urlopen``; no real network is used in tests.
    """
    if method not in ("GET", "POST", "DELETE"):
        raise ValueError("Unsupported HTTP method.")
    headers = {
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
    }
    data = None
    if json_body is not None:
        data = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    retry_header = None
    try:
        req = Request(url, data=data, headers=headers, method=method)
        # urllib redirects copy normal headers, but not unredirected headers.
        # Never forward the bot credential to a redirected endpoint.
        req.add_unredirected_header("Authorization", "Bot " + bot_token)
        try:
            with urlopen(req, timeout=NETWORK_TIMEOUT_SECONDS) as resp:  # noqa: S310 - fixed https URL
                status = int(getattr(resp, "status", 200))
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            status = int(exc.code)
            retry_header = exc.headers.get("Retry-After") if exc.headers else None
            try:
                raw = exc.read(MAX_RESPONSE_BYTES + 1)
            except (OSError, http.client.HTTPException):
                raw = b""
            finally:
                exc.close()
    except (URLError, TimeoutError, OSError, http.client.HTTPException):
        raise DiscordOAuthError("Could not reach Discord.") from None

    payload = None
    if len(raw) <= MAX_RESPONSE_BYTES:
        try:
            parsed = json.loads(raw.decode("utf-8"))
            payload = parsed if isinstance(parsed, dict) else None
        except (UnicodeDecodeError, ValueError):
            payload = None
    return {
        "status": status,
        "payload": payload,
        "retry_after": _parse_retry_after(retry_header, payload) if status == 429 else None,
    }


def _membership_result(status, is_member, retry_after=None):
    result = {"status": status, "is_member": is_member}
    if retry_after is not None:
        result["retry_after"] = retry_after
    return result


def check_guild_membership(settings, discord_user_id):
    """Check whether ``discord_user_id`` is a member of the configured guild.

    Returns ``{"status": ..., "is_member": True | False | None}`` (plus
    ``retry_after`` seconds on HTTP 429). ``is_member`` is True only for
    VERIFIED_MEMBER and False only for a confirmed NOT_IN_GUILD. Every
    uncertain outcome fails closed as CHECK_UNAVAILABLE / BOT_CONFIG_ERROR
    (None). Never raises for Discord/network problems and never returns the
    bot token, headers or raw provider bodies.
    """
    if not isinstance(discord_user_id, str) or not _SNOWFLAKE_RE.match(discord_user_id):
        return _membership_result(STATUS_CHECK_UNAVAILABLE, None)

    url = DISCORD_GUILD_MEMBER_URL.format(guild_id=settings["guild_id"], user_id=discord_user_id)
    try:
        response = _bot_http_request(url, settings["bot_token"])
        status = response["status"]
        payload = response["payload"]
    except (DiscordOAuthError, KeyError, TypeError):
        return _membership_result(STATUS_CHECK_UNAVAILABLE, None)

    code = payload.get("code") if isinstance(payload, dict) else None
    code = code if isinstance(code, int) and not isinstance(code, bool) else None

    if status == 200:
        member_user = payload.get("user") if isinstance(payload, dict) else None
        well_formed = (
            isinstance(member_user, dict)
            and member_user.get("id") == discord_user_id
            and isinstance(payload.get("roles", []), list)
        )
        if well_formed:
            return _membership_result(STATUS_VERIFIED_MEMBER, True)
        return _membership_result(STATUS_CHECK_UNAVAILABLE, None)
    if status == 404:
        if code == DISCORD_ERROR_UNKNOWN_MEMBER:
            return _membership_result(STATUS_NOT_IN_GUILD, False)
        if code == DISCORD_ERROR_UNKNOWN_GUILD:
            return _membership_result(STATUS_BOT_CONFIG_ERROR, None)  # wrong guild id
        return _membership_result(STATUS_CHECK_UNAVAILABLE, None)  # never assume non-member
    if status in (401, 403):
        return _membership_result(STATUS_BOT_CONFIG_ERROR, None)
    if status == 429:
        return _membership_result(STATUS_CHECK_UNAVAILABLE, None, response.get("retry_after"))
    return _membership_result(STATUS_CHECK_UNAVAILABLE, None)


DISCORD_GUILD_CHANNELS_URL = "https://discord.com/api/v10/guilds/{guild_id}/channels"
DISCORD_CHANNEL_URL = "https://discord.com/api/v10/channels/{channel_id}"

CHANNEL_TYPE_GUILD_VOICE = 2
OVERWRITE_TYPE_ROLE = 0
OVERWRITE_TYPE_MEMBER = 1

PERMISSION_VIEW_CHANNEL = 1024
PERMISSION_CONNECT = 1048576
PERMISSION_SPEAK = 2097152
PERMISSION_STREAM = 512
PERMISSION_USE_VAD = 33554432

EVERYONE_DENY = PERMISSION_VIEW_CHANNEL | PERMISSION_CONNECT
PARTICIPANT_ALLOW = (
    PERMISSION_VIEW_CHANNEL
    | PERMISSION_CONNECT
    | PERMISSION_SPEAK
    | PERMISSION_STREAM
    | PERMISSION_USE_VAD
)

MAX_CHANNEL_NAME_LENGTH = 100  # Discord's own limit for channel names

# Result statuses (the ``status`` key of create/delete results).
VOICE_CHANNEL_CREATED = "CHANNEL_CREATED"
VOICE_CHANNEL_DELETED = "CHANNEL_DELETED"
VOICE_CHANNEL_ALREADY_ABSENT = "CHANNEL_ALREADY_ABSENT"
VOICE_REQUEST_REJECTED = "REQUEST_REJECTED"          # HTTP 400 / other 4xx: nothing created
VOICE_BOT_FORBIDDEN = "BOT_FORBIDDEN"                # HTTP 401 / 403
VOICE_GUILD_NOT_FOUND = "GUILD_NOT_FOUND"            # HTTP 404 on create
VOICE_RATE_LIMITED = "RATE_LIMITED"                  # HTTP 429
VOICE_UNAVAILABLE = "UNAVAILABLE"                    # HTTP 5xx / delete network failure
VOICE_OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"            # create may have succeeded on Discord

_CHANNEL_NAME_RE = re.compile(r"[A-Za-z0-9 _-]{1,%d}" % MAX_CHANNEL_NAME_LENGTH)
_STRICT_SNOWFLAKE_RE = re.compile(r"[0-9]{15,25}")  # always used with fullmatch()


class DiscordChannelValidationError(ValueError):
    """A channel name or Discord ID failed validation. Raised before any network call.

    Messages are fixed text: they never echo the rejected value.
    """


def _is_snowflake(value):
    return isinstance(value, str) and _STRICT_SNOWFLAKE_RE.fullmatch(value) is not None


def _validate_bot_settings(settings):
    """Return ``(guild_id, bot_token)`` from ``get_bot_settings`` output or raise."""
    guild_id = settings.get("guild_id") if isinstance(settings, dict) else None
    bot_token = settings.get("bot_token") if isinstance(settings, dict) else None
    if not _is_snowflake(guild_id):
        raise DiscordConfigurationError("DISCORD_GUILD_ID is not a valid Discord ID.")
    if not isinstance(bot_token, str) or _BOT_TOKEN_RE.fullmatch(bot_token) is None:
        raise DiscordConfigurationError("DISCORD_BOT_TOKEN is not usable.")
    return guild_id, bot_token


def validate_channel_name(channel_name):
    """Return ``channel_name`` if it is a safe generated name, else raise.

    Allowed: ASCII letters, digits, space, hyphen, underscore; 1-100 chars; no
    leading/trailing space; at least one letter or digit. Callers should build
    names from internal identifiers only, never booking notes or student details.
    """
    if (
        not isinstance(channel_name, str)
        or _CHANNEL_NAME_RE.fullmatch(channel_name) is None
        or channel_name != channel_name.strip()
        or not any(ch.isalnum() for ch in channel_name)
    ):
        raise DiscordChannelValidationError("Invalid voice channel name.")
    return channel_name


def build_private_voice_overwrites(guild_id, student_discord_id, provider_discord_id):
    """Return the three required private overwrites (decimal-string bitmasks).

    @everyone is denied VIEW_CHANNEL | CONNECT; the student and the provider are
    each explicitly allowed. Raises DiscordChannelValidationError for invalid
    IDs, identical participants, or a participant ID equal to the guild ID, so a
    permissive or malformed channel can never be built.
    """
    for value in (guild_id, student_discord_id, provider_discord_id):
        if not _is_snowflake(value):
            raise DiscordChannelValidationError("Invalid Discord ID.")
    if student_discord_id == provider_discord_id:
        raise DiscordChannelValidationError("Student and provider must be different Discord accounts.")
    if guild_id in (student_discord_id, provider_discord_id):
        raise DiscordChannelValidationError("Invalid Discord ID.")
    return [
        {"id": guild_id, "type": OVERWRITE_TYPE_ROLE, "allow": "0", "deny": str(EVERYONE_DENY)},
        {"id": student_discord_id, "type": OVERWRITE_TYPE_MEMBER, "allow": str(PARTICIPANT_ALLOW), "deny": "0"},
        {"id": provider_discord_id, "type": OVERWRITE_TYPE_MEMBER, "allow": str(PARTICIPANT_ALLOW), "deny": "0"},
    ]


def _voice_result(status, ok=False, channel_id=None, outcome_unknown=False, retry_after=None,
                  unverified_channel_id=None, already_absent=None):
    result = {"ok": ok, "status": status, "channel_id": channel_id, "outcome_unknown": outcome_unknown}
    if already_absent is not None:
        result["already_absent"] = already_absent
    if retry_after is not None:
        result["retry_after"] = retry_after
    if unverified_channel_id is not None:
        result["unverified_channel_id"] = unverified_channel_id
    return result


def _permission_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}", value):
        return int(value)
    return None


def _overwrite_set(items):
    """Normalise overwrites to a set of (id, type, allow, deny) or None if malformed."""
    if not isinstance(items, list):
        return None
    normalised = set()
    for item in items:
        if not isinstance(item, dict):
            return None
        overwrite_id, kind = item.get("id"), item.get("type")
        allow, deny = _permission_int(item.get("allow")), _permission_int(item.get("deny"))
        if not isinstance(overwrite_id, str) or isinstance(kind, bool) or not isinstance(kind, int):
            return None
        if allow is None or deny is None:
            return None
        normalised.add((overwrite_id, kind, allow, deny))
    return normalised if len(normalised) == len(items) else None


def _channel_matches(payload, guild_id, expected_overwrites):
    """True only if the created-channel object proves what we asked for."""
    if not isinstance(payload, dict) or not _is_snowflake(payload.get("id")):
        return False
    kind = payload.get("type")
    if isinstance(kind, bool) or kind != CHANNEL_TYPE_GUILD_VOICE:
        return False
    if payload.get("guild_id") != guild_id:
        return False
    actual = _overwrite_set(payload.get("permission_overwrites"))
    expected = _overwrite_set(expected_overwrites)
    return actual is not None and expected is not None and actual == expected


def create_private_voice_channel(settings, channel_name, student_discord_id, provider_discord_id):
    """Create a private guild voice channel for exactly two participants.

    ``settings`` is the output of ``get_bot_settings``. Returns a dict:
    ``{"ok", "status", "channel_id", "outcome_unknown"}`` (plus ``retry_after``
    on 429, ``unverified_channel_id`` when Discord answered 2xx but the channel
    could not be validated). ``ok`` is True and ``channel_id`` set ONLY for an
    HTTP 201 whose channel object validates (id, guild, type 2 and exactly the
    three expected overwrites).

    ``outcome_unknown`` is True whenever a channel may exist on Discord although
    success was not confirmed: timeouts/network failures, unreadable or
    unvalidated 2xx responses, and 5xx. Callers must treat that as a possible
    orphan (reconcile or clean up); this helper never retries and never sleeps.

    Raises DiscordConfigurationError / DiscordChannelValidationError for bad
    settings or input, before any network call. Never raises for Discord or
    network problems and never returns the bot token or raw provider bodies.
    """
    guild_id, bot_token = _validate_bot_settings(settings)
    name = validate_channel_name(channel_name)
    overwrites = build_private_voice_overwrites(guild_id, student_discord_id, provider_discord_id)
    body = {
        "name": name,
        "type": CHANNEL_TYPE_GUILD_VOICE,
        "permission_overwrites": overwrites,
    }
    url = DISCORD_GUILD_CHANNELS_URL.format(guild_id=guild_id)

    try:
        response = _bot_api_request("POST", url, bot_token, body)
        status = int(response["status"])
        payload = response.get("payload")
        retry_after = response.get("retry_after")
    except Exception:  # noqa: BLE001 - any failure after sending is an ambiguous outcome
        return _voice_result(VOICE_OUTCOME_UNKNOWN, outcome_unknown=True)

    if status == 201:
        if _channel_matches(payload, guild_id, overwrites) and payload.get("name") == name:
            return _voice_result(VOICE_CHANNEL_CREATED, ok=True, channel_id=payload["id"])
        unverified = payload.get("id") if isinstance(payload, dict) and _is_snowflake(payload.get("id")) else None
        return _voice_result(VOICE_OUTCOME_UNKNOWN, outcome_unknown=True, unverified_channel_id=unverified)
    if 200 <= status < 300:
        return _voice_result(VOICE_OUTCOME_UNKNOWN, outcome_unknown=True)
    if status in (401, 403):
        return _voice_result(VOICE_BOT_FORBIDDEN)
    if status == 404:
        return _voice_result(VOICE_GUILD_NOT_FOUND)
    if status == 429:
        return _voice_result(VOICE_RATE_LIMITED, retry_after=retry_after)
    if 400 <= status < 500:  # includes 400
        return _voice_result(VOICE_REQUEST_REJECTED)
    if 500 <= status < 600:
        return _voice_result(VOICE_UNAVAILABLE, outcome_unknown=True)
    return _voice_result(VOICE_OUTCOME_UNKNOWN, outcome_unknown=True)


def delete_voice_channel(settings, channel_id):
    """Delete the channel with snowflake ``channel_id`` (single attempt, no retries).

    Returns ``{"ok", "status", "channel_id", "outcome_unknown"}`` plus
    ``already_absent`` and, on 429, ``retry_after``. HTTP 204 (and 200, which is
    what Discord actually returns with the deleted channel object) -> deleted;
    404 -> ok / already absent (idempotent). The URL is always built from the
    fixed endpoint and a validated snowflake; arbitrary URLs are never accepted.

    LOW-LEVEL ONLY: callers must authorise the user and prove that Learn2Play
    created this channel before calling.
    """
    _, bot_token = _validate_bot_settings(settings)
    if not _is_snowflake(channel_id):
        raise DiscordChannelValidationError("Invalid Discord channel ID.")
    url = DISCORD_CHANNEL_URL.format(channel_id=channel_id)

    try:
        response = _bot_api_request("DELETE", url, bot_token)
        status = int(response["status"])
        payload = response.get("payload")
        retry_after = response.get("retry_after")
    except Exception:  # noqa: BLE001
        return _voice_result(VOICE_UNAVAILABLE, outcome_unknown=True, already_absent=False)

    if status == 204 or (status == 200 and isinstance(payload, dict) and payload.get("id") == channel_id):
        return _voice_result(VOICE_CHANNEL_DELETED, ok=True, channel_id=channel_id, already_absent=False)
    if status == 404:
        return _voice_result(VOICE_CHANNEL_ALREADY_ABSENT, ok=True, channel_id=channel_id, already_absent=True)
    if status in (401, 403):
        return _voice_result(VOICE_BOT_FORBIDDEN, already_absent=False)
    if status == 429:
        return _voice_result(VOICE_RATE_LIMITED, retry_after=retry_after, already_absent=False)
    if 400 <= status < 500:
        return _voice_result(VOICE_REQUEST_REJECTED, already_absent=False)
    return _voice_result(VOICE_UNAVAILABLE, outcome_unknown=status >= 500 or 200 <= status < 300, already_absent=False)
