"""Tests for create_app(test_config) isolation and Config compatibility.

Safety properties of this module:
  * Every app is built from an in-memory SQLite configuration.
  * Rejected configurations use DUMMY values only (reserved ".invalid" hosts).
  * Socket connection and DNS lookups are patched to fail the test if attempted.
  * Production-configuration tests run config.py in child processes with a
    scrubbed environment; they never create an app or touch a database.
"""
import copy
import json
import os
import socket
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url

try:
    from Backend import (
        UnsafeTestConfigError,
        _VERIFIED_MEMORY_SQLITE_URIS,
        _is_in_memory_sqlite_url,
        _prepare_test_config,
        create_app,
    )
    from Backend import config as config_module
    from Backend.extensions import db
except ImportError:
    from __init__ import (
        UnsafeTestConfigError,
        _VERIFIED_MEMORY_SQLITE_URIS,
        _is_in_memory_sqlite_url,
        _prepare_test_config,
        create_app,
    )
    import config as config_module
    from extensions import db

Config = config_module.Config
CONFIG_PATH = Path(config_module.__file__).resolve()
BACKEND_DIR = CONFIG_PATH.parent

MEMORY_URI = "sqlite:///:memory:"
PENDING_MEMORY_URIS = ("sqlite://", "sqlite:///", "sqlite+pysqlite:///:memory:")
CANARY = "CANARY-PASSWORD-DO-NOT-LEAK"
PG_TAIL = "u:dummy-pass@db.invalid:6543/postgres"
GUARD_PROBE_FILE = "l2p_guard_probe_must_not_exist.db"


def _postgres_options():
    return {
        "pool_pre_ping": True,
        "pool_size": 1,
        "max_overflow": 0,
        "connect_args": {"sslmode": "require", "prepare_threshold": None},
    }


def _sqlite_options():
    return {"pool_pre_ping": True}


def _config_snapshot():
    return copy.deepcopy((Config.SQLALCHEMY_DATABASE_URI, Config.SQLALCHEMY_ENGINE_OPTIONS))


# ---------------------------------------------------------------------------
# Pure helpers in config.py
# ---------------------------------------------------------------------------
class ConfigHelperTests(unittest.TestCase):
    NORMALIZATION_CASES = (
        (None, None),
        ("", ""),
        ("   ", "   "),
        ("postgres://" + PG_TAIL, "postgresql+psycopg://" + PG_TAIL),
        ("postgresql://" + PG_TAIL, "postgresql+psycopg://" + PG_TAIL),
        ("postgresql+psycopg2://" + PG_TAIL, "postgresql+psycopg://" + PG_TAIL),
        ("postgresql+psycopg://" + PG_TAIL, "postgresql+psycopg://" + PG_TAIL),
        # Known quirks (OD-1) that must be preserved exactly.
        ("POSTGRES://" + PG_TAIL, "POSTGRES://" + PG_TAIL),
        ("Postgresql://" + PG_TAIL, "Postgresql://" + PG_TAIL),
        (" postgres://" + PG_TAIL, " postgres://" + PG_TAIL),
        ("postgresql+asyncpg://" + PG_TAIL, "postgresql+asyncpg://" + PG_TAIL),
        ("sqlite:///relative.db", "sqlite:///relative.db"),
        ("mysql://" + PG_TAIL, "mysql://" + PG_TAIL),
    )

    def test_normalize_database_url_table(self):
        for value, expected in self.NORMALIZATION_CASES:
            with self.subTest(value=value):
                self.assertEqual(config_module.normalize_database_url(value), expected)

    def test_build_engine_options_table(self):
        cases = (
            ("postgresql+psycopg://" + PG_TAIL, _postgres_options()),
            ("sqlite:///:memory:", _sqlite_options()),
            ("sqlite:///some/file.db", _sqlite_options()),
            ("postgresql://" + PG_TAIL, _sqlite_options()),  # not yet normalized
            ("mysql://" + PG_TAIL, _sqlite_options()),
            ("", _sqlite_options()),
            (None, _sqlite_options()),
        )
        for uri, expected in cases:
            with self.subTest(uri=uri):
                self.assertEqual(config_module.build_engine_options(uri), expected)

    def test_build_engine_options_returns_fresh_objects(self):
        uri = "postgresql+psycopg://" + PG_TAIL
        first = config_module.build_engine_options(uri)
        second = config_module.build_engine_options(uri)
        self.assertIsNot(first, second)
        self.assertIsNot(first["connect_args"], second["connect_args"])
        first["connect_args"]["sslmode"] = "disable"
        first["pool_size"] = 99
        self.assertEqual(second, _postgres_options())
        self.assertEqual(config_module.build_engine_options(uri), _postgres_options())

    def test_helpers_do_not_mutate_their_input(self):
        original = "postgres://" + PG_TAIL
        value = str(original)
        config_module.normalize_database_url(value)
        self.assertEqual(value, original)


# ---------------------------------------------------------------------------
# Production configuration, evaluated in child processes (golden tests).
# Expected values were derived from the ORIGINAL config.py logic, not from the
# refactored code.
# ---------------------------------------------------------------------------
_CONFIG_ENV_KEYS = (
    "DATABASE_URL", "APP_ENV", "VERCEL", "SECRET_KEY", "JAAS_APP_ID",
    "JAAS_API_KEY_ID", "JAAS_PRIVATE_KEY", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY",
)

_PROBE_SCRIPT = (
    "import importlib.util, json, sys\n"
    "spec = importlib.util.spec_from_file_location('l2p_config_probe', sys.argv[1])\n"
    "module = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(module)\n"
    "c = module.Config\n"
    "print(json.dumps({'uri': c.SQLALCHEMY_DATABASE_URI,\n"
    "  'options': c.SQLALCHEMY_ENGINE_OPTIONS,\n"
    "  'track': c.SQLALCHEMY_TRACK_MODIFICATIONS,\n"
    "  'secret_key': c.SECRET_KEY,\n"
    "  'jaas': [c.JAAS_APP_ID, c.JAAS_API_KEY_ID, c.JAAS_PRIVATE_KEY]}))\n"
)

_FALLBACK = object()  # means: the default SQLite file next to config.py
_PG = "postgres"
_SQLITE = "sqlite"

GOLDEN_DATABASE_URL_SCENARIOS = (
    ("unset", None, _FALLBACK, _SQLITE),
    ("empty", "", _FALLBACK, _SQLITE),
    ("whitespace only", "   ", "   ", _SQLITE),
    ("postgres scheme", "postgres://" + PG_TAIL, "postgresql+psycopg://" + PG_TAIL, _PG),
    ("postgresql scheme", "postgresql://" + PG_TAIL, "postgresql+psycopg://" + PG_TAIL, _PG),
    ("psycopg2 scheme", "postgresql+psycopg2://" + PG_TAIL, "postgresql+psycopg://" + PG_TAIL, _PG),
    ("psycopg scheme", "postgresql+psycopg://" + PG_TAIL, "postgresql+psycopg://" + PG_TAIL, _PG),
    ("uppercase scheme", "POSTGRES://" + PG_TAIL, "POSTGRES://" + PG_TAIL, _SQLITE),
    ("mixed-case scheme", "Postgresql://" + PG_TAIL, "Postgresql://" + PG_TAIL, _SQLITE),
    ("leading whitespace", " postgres://" + PG_TAIL, " postgres://" + PG_TAIL, _SQLITE),
    # Trailing whitespace is not stripped but the prefix still matches.
    ("trailing whitespace", "postgres://" + PG_TAIL + " ", "postgresql+psycopg://" + PG_TAIL + " ", _PG),
    ("asyncpg scheme", "postgresql+asyncpg://" + PG_TAIL, "postgresql+asyncpg://" + PG_TAIL, _SQLITE),
    ("sqlite relative", "sqlite:///relative.db", "sqlite:///relative.db", _SQLITE),
    ("sqlite memory", "sqlite:///:memory:", "sqlite:///:memory:", _SQLITE),
    ("mysql", "mysql://" + PG_TAIL, "mysql://" + PG_TAIL, _SQLITE),
)


def _run_config_probe(extra_env):
    env = {key: value for key, value in os.environ.items() if key not in _CONFIG_ENV_KEYS}
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", _PROBE_SCRIPT, str(CONFIG_PATH)],
        env=env, capture_output=True, text=True, timeout=120, cwd=str(BACKEND_DIR),
    )


def _probe_ok(extra_env):
    done = _run_config_probe(extra_env)
    if done.returncode != 0:
        raise AssertionError("config probe failed: %s" % done.stderr.strip().splitlines()[-1:])
    return json.loads(done.stdout.strip().splitlines()[-1])


class ProductionConfigGoldenTests(unittest.TestCase):
    def test_database_url_scenarios_match_original_behaviour(self):
        fallback_uri = "sqlite:///" + os.path.join(str(BACKEND_DIR), "learn2play.db")
        for label, env_value, expected_uri, options_kind in GOLDEN_DATABASE_URL_SCENARIOS:
            with self.subTest(scenario=label):
                env = {} if env_value is None else {"DATABASE_URL": env_value}
                result = _probe_ok(env)
                self.assertEqual(
                    result["uri"], fallback_uri if expected_uri is _FALLBACK else expected_uri
                )
                expected_options = _postgres_options() if options_kind == _PG else _sqlite_options()
                self.assertEqual(result["options"], expected_options)
                self.assertIs(result["track"], False)

    def test_production_requires_a_long_secret_key(self):
        failing = (
            ("production, no key", {"APP_ENV": "production"}),
            ("production, 31 chars", {"APP_ENV": "production", "SECRET_KEY": "s" * 31}),
            ("production, blank key", {"APP_ENV": "production", "SECRET_KEY": "   "}),
            ("vercel, no key", {"VERCEL": "1"}),
        )
        for label, env in failing:
            with self.subTest(scenario=label):
                done = _run_config_probe(env)
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("Production requires a SECRET_KEY", done.stderr)

    def test_secret_key_accepted_and_stripped(self):
        self.assertEqual(
            _probe_ok({"APP_ENV": "production", "SECRET_KEY": "s" * 32})["secret_key"], "s" * 32
        )
        self.assertEqual(
            _probe_ok({"APP_ENV": " Production ", "SECRET_KEY": "  " + "s" * 32 + "  "})["secret_key"],
            "s" * 32,
        )
        self.assertEqual(_probe_ok({"VERCEL": "1", "SECRET_KEY": "v" * 40})["secret_key"], "v" * 40)

    def test_development_secret_is_random_per_process(self):
        first = _probe_ok({})["secret_key"]
        second = _probe_ok({})["secret_key"]
        self.assertGreaterEqual(len(first), 32)
        self.assertGreaterEqual(len(second), 32)
        self.assertNotEqual(first, second)

    def test_jaas_values_are_stripped_and_pem_newlines_normalised(self):
        result = _probe_ok({
            "JAAS_APP_ID": "  app-id  ",
            "JAAS_API_KEY_ID": " key-id ",
            "JAAS_PRIVATE_KEY": "line1\\nline2",  # backslash + n, as Vercel supplies it
        })
        self.assertEqual(result["jaas"], ["app-id", "key-id", "line1\nline2"])


# ---------------------------------------------------------------------------
# create_app(test_config)
# ---------------------------------------------------------------------------
class TestConfigGuardTests(unittest.TestCase):
    def setUp(self):
        for patcher in (
            mock.patch.object(
                socket.socket, "connect", side_effect=AssertionError("network connection attempted")
            ),
            mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS lookup attempted")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self._class_config_before = _config_snapshot()
        self.addCleanup(self._assert_class_config_unchanged)

    def _assert_class_config_unchanged(self):
        self.assertEqual(_config_snapshot(), self._class_config_before)

    # -- helpers ---------------------------------------------------------
    def _config(self, **overrides):
        config = {
            "TESTING": True,
            "WTF_CSRF_ENABLED": False,
            "SQLALCHEMY_DATABASE_URI": MEMORY_URI,
        }
        config.update(overrides)
        return config

    def _assert_rejected(self, test_config, canary=None):
        """create_app must refuse test_config before db.init_app is reached."""
        with mock.patch.object(db, "init_app") as init_app:
            with self.assertRaises(UnsafeTestConfigError) as caught:
                create_app(test_config)
        init_app.assert_not_called()
        self.assertIsNone(caught.exception.__cause__)
        if canary:
            self.assertNotIn(canary, str(caught.exception))
        return caught.exception

    def _dispose(self, app):
        with app.app_context():
            db.session.remove()
            db.engine.dispose()

    @staticmethod
    def _database_files(engine):
        with engine.connect() as connection:
            rows = connection.exec_driver_sql("PRAGMA database_list").fetchall()
        return [row[2] for row in rows]

    # -- acceptance ------------------------------------------------------
    def test_verified_memory_uri_is_accepted_and_really_in_memory(self):
        app = create_app(self._config())
        self.addCleanup(self._dispose, app)
        self.assertTrue(app.config["TESTING"])
        self.assertEqual(app.config["SQLALCHEMY_DATABASE_URI"], MEMORY_URI)
        with app.app_context():
            engine = db.engine
            self.assertEqual(engine.url.drivername, "sqlite")
            self.assertEqual(engine.url.database, ":memory:")
            self.assertEqual(self._database_files(engine), [""])

    def test_uri_validated_is_the_uri_sqlalchemy_uses(self):
        app = create_app(self._config())
        self.addCleanup(self._dispose, app)
        with app.app_context():
            self.assertEqual(db.engine.url, make_url(app.config["SQLALCHEMY_DATABASE_URI"]))

    def test_init_app_is_called_once_for_an_accepted_config(self):
        with mock.patch.object(db, "init_app", wraps=db.init_app) as init_app:
            app = create_app(self._config())
        self.addCleanup(self._dispose, app)
        init_app.assert_called_once_with(app)

    def test_allowed_settings_pass_through(self):
        app = create_app(self._config(
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SQLALCHEMY_ECHO=False,
            SECRET_KEY="k" * 40,
            JAAS_APP_ID="test-app",
        ))
        self.addCleanup(self._dispose, app)
        self.assertEqual(app.config["SECRET_KEY"], "k" * 40)
        self.assertEqual(app.config["JAAS_APP_ID"], "test-app")
        self.assertIs(app.config["SQLALCHEMY_ECHO"], False)

    def test_verified_allowlist_contains_only_the_verified_form(self):
        self.assertEqual(_VERIFIED_MEMORY_SQLITE_URIS, frozenset({MEMORY_URI}))

    # -- invalid or missing URI -------------------------------------------
    def test_explicit_uri_is_required(self):
        for test_config in ({}, {"TESTING": True}, {"TESTING": True, "JAAS_APP_ID": "x"}):
            with self.subTest(test_config=test_config):
                self._assert_rejected(test_config)

    def test_invalid_uri_values_are_rejected(self):
        values = (
            None, "", "   ", "\t\n", 123, 1.5, True, b"sqlite:///:memory:",
            [MEMORY_URI], (MEMORY_URI,), {"url": MEMORY_URI}, make_url(MEMORY_URI), object(),
        )
        for value in values:
            with self.subTest(value=repr(value)):
                self._assert_rejected(self._config(SQLALCHEMY_DATABASE_URI=value))

    def test_uri_with_surrounding_whitespace_is_rejected_not_stripped(self):
        for value in (" " + MEMORY_URI, MEMORY_URI + " ", MEMORY_URI + "\n", "\t" + MEMORY_URI):
            with self.subTest(value=repr(value)):
                self._assert_rejected(self._config(SQLALCHEMY_DATABASE_URI=value))

    def test_non_mapping_test_config_is_rejected(self):
        for value in (MEMORY_URI, [MEMORY_URI], 5, True):
            with self.subTest(value=repr(value)):
                self._assert_rejected(value)

    def test_non_string_config_keys_are_rejected(self):
        self._assert_rejected({1: "x", "SQLALCHEMY_DATABASE_URI": MEMORY_URI})

    def test_uri_is_normalised_in_the_copy_only(self):
        caller = {"SQLALCHEMY_DATABASE_URI": "postgres://" + PG_TAIL}
        snapshot = copy.deepcopy(caller)
        prepared = _prepare_test_config(caller)
        self.assertEqual(prepared["SQLALCHEMY_DATABASE_URI"], "postgresql+psycopg://" + PG_TAIL)
        self.assertEqual(caller, snapshot)
        self.assertIsNot(prepared, caller)

    def test_malformed_uris_are_rejected_without_leaking_the_value(self):
        for value in (
            "not a url " + CANARY,
            "://" + CANARY,
            "sqlite:/" + CANARY,
            "postgresql://user:" + CANARY + "@[bad-host/db",
        ):
            with self.subTest(value="<malformed>"):
                self._assert_rejected(self._config(SQLALCHEMY_DATABASE_URI=value), canary=CANARY)

    # -- SQLite spellings --------------------------------------------------
    def test_spellings_pending_verification_are_currently_rejected(self):
        for value in PENDING_MEMORY_URIS:
            with self.subTest(value=value):
                self._assert_rejected(self._config(SQLALCHEMY_DATABASE_URI=value))

    def test_file_based_sqlite_is_rejected_and_creates_nothing(self):
        values = (
            "sqlite:///learn2play.db",
            "sqlite:///" + GUARD_PROBE_FILE,
            "sqlite:///./" + GUARD_PROBE_FILE,
            "sqlite:///../" + GUARD_PROBE_FILE,
            "sqlite:////tmp/" + GUARD_PROBE_FILE,
            "sqlite:///C:/Users/dummy/" + GUARD_PROBE_FILE,
            "sqlite:///C:\\Users\\dummy\\" + GUARD_PROBE_FILE,
            "sqlite:///\\\\server\\share\\" + GUARD_PROBE_FILE,
            "sqlite:///file:" + GUARD_PROBE_FILE + "?uri=true",
            "sqlite:///file:memdb1?mode=memory&cache=shared&uri=true",
            "sqlite:///:memory:?cache=shared",
            "sqlite:///:memory:?uri=true",
            "sqlite:///?mode=memory",
            "sqlite+pysqlite:///" + GUARD_PROBE_FILE,
            "sqlite+aiosqlite:///:memory:",
            "sqlite+pysqlcipher:///:memory:",
            "sqlite://user:pw@host/" + GUARD_PROBE_FILE,
        )
        for value in values:
            with self.subTest(value=value):
                self._assert_rejected(self._config(SQLALCHEMY_DATABASE_URI=value))
        self.assertFalse((Path.cwd() / GUARD_PROBE_FILE).exists())
        self.assertFalse((BACKEND_DIR / GUARD_PROBE_FILE).exists())
        self.assertFalse((BACKEND_DIR.parent / GUARD_PROBE_FILE).exists())

    # -- non-SQLite --------------------------------------------------------
    def test_non_sqlite_databases_are_rejected_without_leaking_credentials(self):
        values = (
            "postgresql://u:%s@db.invalid:5432/postgres" % CANARY,
            "postgres://u:%s@db.invalid:5432/postgres" % CANARY,
            "postgresql+psycopg://u:%s@db.invalid:6543/postgres" % CANARY,
            "postgresql+psycopg2://u:%s@db.invalid:5432/postgres" % CANARY,
            # Supabase-shaped dummy pooler URL.
            "postgresql://postgres.dummyref:%s@aws-0-dummy.pooler.supabase.invalid:6543/postgres" % CANARY,
            "postgresql://u:%s@localhost:5432/test" % CANARY,
            "postgresql://u:%s@127.0.0.1:5432/test" % CANARY,
            "mysql://u:%s@db.invalid/x" % CANARY,
            "mysql+pymysql://u:%s@db.invalid/x" % CANARY,
            "mssql+pyodbc://u:%s@db.invalid/x" % CANARY,
            "oracle://u:%s@db.invalid/x" % CANARY,
            "mongodb://u:%s@db.invalid/x" % CANARY,
        )
        for value in values:
            with self.subTest(scheme=value.split(":")[0]):
                self._assert_rejected(self._config(SQLALCHEMY_DATABASE_URI=value), canary=CANARY)

    # -- binds and engine options -------------------------------------------
    def test_secondary_binds_are_rejected(self):
        values = (
            {"other": "postgresql://u:%s@db.invalid/x" % CANARY},
            {"other": MEMORY_URI},
            {},
            None,
        )
        for value in values:
            with self.subTest(value=repr(value)[:20]):
                self._assert_rejected(self._config(SQLALCHEMY_BINDS=value), canary=CANARY)
        self._assert_rejected(self._config(sqlalchemy_binds={"other": MEMORY_URI}))
        self._assert_rejected(self._config(SQLALCHEMY_ENGINES={"default": MEMORY_URI}))

    def test_caller_supplied_engine_options_are_rejected(self):
        values = (
            {}, None, {"pool_pre_ping": True}, {"pool_size": 1},
            {"connect_args": {"sslmode": "require"}}, _postgres_options(),
        )
        for value in values:
            with self.subTest(value=repr(value)[:30]):
                self._assert_rejected(self._config(SQLALCHEMY_ENGINE_OPTIONS=value))
        self._assert_rejected(self._config(sqlalchemy_engine_options={"pool_pre_ping": True}))

    def test_unsupported_sqlalchemy_settings_are_rejected(self):
        for key in (
            "SQLALCHEMY_RECORD_QUERIES", "SQLALCHEMY_COMMIT_ON_TEARDOWN",
            "SQLALCHEMY_SESSION_OPTIONS", "SQLALCHEMY_NATIVE_UNICODE", "sqlalchemy_database_uri",
        ):
            with self.subTest(key=key):
                self._assert_rejected(self._config(**{key: True}))

    def test_inherited_unsafe_sqlalchemy_settings_are_rejected_after_merge(self):
        inherited_bind = {"other": "postgresql://u:%s@db.invalid/x" % CANARY}
        with mock.patch.object(Config, "SQLALCHEMY_BINDS", inherited_bind, create=True):
            self._assert_rejected(self._config(), canary=CANARY)
        with mock.patch.object(Config, "SQLALCHEMY_ENGINES", {"default": MEMORY_URI}, create=True):
            self._assert_rejected(self._config())

    def test_engine_options_are_safe_sqlite_options(self):
        app = create_app(self._config())
        self.addCleanup(self._dispose, app)
        options = app.config["SQLALCHEMY_ENGINE_OPTIONS"]
        self.assertIs(options["pool_pre_ping"], True)
        self.assertNotIn("pool_size", options)
        self.assertNotIn("max_overflow", options)
        self.assertNotIn("sslmode", options.get("connect_args", {}))
        self.assertNotIn("prepare_threshold", options.get("connect_args", {}))
        self.assertIsNot(options, Config.SQLALCHEMY_ENGINE_OPTIONS)

    def test_postgres_shaped_class_config_cannot_leak_into_sqlite(self):
        shared = _postgres_options()
        pg_uri = "postgresql+psycopg://" + PG_TAIL
        with mock.patch.multiple(
            Config, SQLALCHEMY_DATABASE_URI=pg_uri, SQLALCHEMY_ENGINE_OPTIONS=shared
        ):
            app = create_app(self._config())
            self.addCleanup(self._dispose, app)
            options = app.config["SQLALCHEMY_ENGINE_OPTIONS"]
            self.assertNotIn("pool_size", options)
            self.assertNotIn("max_overflow", options)
            self.assertNotIn("sslmode", options.get("connect_args", {}))
            with app.app_context():
                self.assertEqual(db.engine.url.drivername, "sqlite")
                self.assertEqual(self._database_files(db.engine), [""])
            self.assertEqual(shared, _postgres_options())  # never mutated

    # -- caller configuration is never modified ------------------------------
    def test_callers_config_is_not_modified_or_shared(self):
        caller = self._config(JAAS_EXTRA={"nested": ["a", "b"]})
        snapshot = copy.deepcopy(caller)
        app = create_app(caller)
        self.addCleanup(self._dispose, app)
        self.assertEqual(caller, snapshot)
        self.assertIsNot(app.config["JAAS_EXTRA"], caller["JAAS_EXTRA"])
        self.assertIsNot(app.config["JAAS_EXTRA"]["nested"], caller["JAAS_EXTRA"]["nested"])
        app.config["JAAS_EXTRA"]["nested"].append("c")
        self.assertEqual(caller, snapshot)

    def test_rejected_config_is_not_modified(self):
        caller = self._config(SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True})
        snapshot = copy.deepcopy(caller)
        self._assert_rejected(caller)
        self.assertEqual(caller, snapshot)

    # -- isolation between apps ---------------------------------------------
    def test_sequential_apps_do_not_share_engines_data_or_options(self):
        app_a = create_app(self._config())
        app_b = create_app(self._config())
        self.addCleanup(self._dispose, app_a)
        self.addCleanup(self._dispose, app_b)
        self.assertIsNot(
            app_a.config["SQLALCHEMY_ENGINE_OPTIONS"], app_b.config["SQLALCHEMY_ENGINE_OPTIONS"]
        )
        with app_a.app_context():
            engine_a = db.engine
            db.create_all()
            tables_a = inspect(engine_a).get_table_names()
        with app_b.app_context():
            engine_b = db.engine
            tables_b = inspect(engine_b).get_table_names()
        self.assertIsNot(engine_a, engine_b)
        self.assertTrue(tables_a)
        self.assertEqual(tables_b, [])

    def test_many_sequential_creations_leave_class_config_untouched(self):
        for _ in range(3):
            app = create_app(self._config())
            self.addCleanup(self._dispose, app)
            with app.app_context():
                db.engine  # force engine creation and driver defaults
        # _assert_class_config_unchanged runs as a cleanup.


# ---------------------------------------------------------------------------
# The normal create_app() path is not subject to the test-only restriction.
# ---------------------------------------------------------------------------
class ProductionFactoryPathTests(unittest.TestCase):
    def test_no_test_config_has_no_sqlite_restriction(self):
        pg_uri = "postgresql+psycopg://" + PG_TAIL
        with mock.patch.multiple(
            Config, SQLALCHEMY_DATABASE_URI=pg_uri, SQLALCHEMY_ENGINE_OPTIONS=_postgres_options()
        ), mock.patch.object(db, "init_app") as init_app:
            app = create_app()
        init_app.assert_called_once_with(app)
        self.assertEqual(app.config["SQLALCHEMY_DATABASE_URI"], pg_uri)
        self.assertEqual(app.config["SQLALCHEMY_ENGINE_OPTIONS"], _postgres_options())

    def test_engine_options_are_deep_copied_per_app(self):
        shared = _postgres_options()
        with mock.patch.multiple(
            Config,
            SQLALCHEMY_DATABASE_URI="postgresql+psycopg://" + PG_TAIL,
            SQLALCHEMY_ENGINE_OPTIONS=shared,
        ), mock.patch.object(db, "init_app"):
            app_one = create_app()
            app_two = create_app()
        for app in (app_one, app_two):
            options = app.config["SQLALCHEMY_ENGINE_OPTIONS"]
            self.assertEqual(options, _postgres_options())
            self.assertIsNot(options, shared)
            self.assertIsNot(options["connect_args"], shared["connect_args"])
        options_one = app_one.config["SQLALCHEMY_ENGINE_OPTIONS"]
        options_two = app_two.config["SQLALCHEMY_ENGINE_OPTIONS"]
        self.assertIsNot(options_one, options_two)
        self.assertIsNot(options_one["connect_args"], options_two["connect_args"])
        options_one["connect_args"]["sslmode"] = "disable"
        self.assertEqual(options_two, _postgres_options())
        self.assertEqual(shared, _postgres_options())

    def test_other_settings_still_come_from_config(self):
        with mock.patch.object(db, "init_app"):
            app = create_app()
        self.assertEqual(app.config["SECRET_KEY"], Config.SECRET_KEY)
        self.assertEqual(app.config["JAAS_APP_ID"], Config.JAAS_APP_ID)
        self.assertIs(app.config["SQLALCHEMY_TRACK_MODIFICATIONS"], False)


# ---------------------------------------------------------------------------
# SQLite URI classification and evidence for the pending spellings.
# ---------------------------------------------------------------------------
class SqliteUriClassificationTests(unittest.TestCase):
    def test_structural_check_accepts_only_memory_sqlite(self):
        accepted = (
            "sqlite://", "sqlite:///", "sqlite:///:memory:",
            "sqlite+pysqlite://", "sqlite+pysqlite:///:memory:",
        )
        rejected = (
            "sqlite:///file.db", "sqlite:////abs/file.db", "sqlite:///C:/x/file.db",
            "sqlite:///:memory:?cache=shared", "sqlite:///file:x?mode=memory&uri=true",
            "sqlite+aiosqlite:///:memory:", "sqlite+pysqlcipher:///:memory:",
            "sqlite://user:pw@host/:memory:", "postgresql://u:dummy@db.invalid/x",
            "mysql://u:dummy@db.invalid/x",
        )
        for value in accepted:
            with self.subTest(accepted=value):
                self.assertTrue(_is_in_memory_sqlite_url(make_url(value)))
        for value in rejected:
            with self.subTest(rejected=value):
                self.assertFalse(_is_in_memory_sqlite_url(make_url(value)))

    def test_candidate_memory_forms_probe(self):
        """EVIDENCE ONLY: this does not widen the allowlist.

        For each candidate spelling, the real dialect must hand the sqlite
        driver ':memory:' and SQLite must report no database file. A reviewer
        may use a clean run to decide to promote a spelling in a later change.
        """
        for value in (MEMORY_URI,) + PENDING_MEMORY_URIS + ("sqlite+pysqlite://",):
            with self.subTest(uri=value):
                engine = create_engine(value)
                try:
                    args, _kwargs = engine.dialect.create_connect_args(make_url(value))
                    self.assertEqual(args[0], ":memory:")
                    with engine.connect() as connection:
                        rows = connection.exec_driver_sql("PRAGMA database_list").fetchall()
                    self.assertEqual([row[2] for row in rows], [""])
                finally:
                    engine.dispose()


if __name__ == "__main__":
    unittest.main()
