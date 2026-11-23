from dataclasses import replace
import json
from pathlib import Path
import sys
from unittest.mock import MagicMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import restore_supabase_development_local as restore


@pytest.mark.parametrize("profile", [restore.LEGACY, restore.CURRENT])
def test_paths_reject_existing_target_and_wrong_backup(tmp_path, profile):
    source, target = tmp_path / profile.source, tmp_path / profile.target
    source.mkdir()
    for name in (restore.ARCHIVE, "manifest.json"):
        (source / name).touch()
    restore.check_paths(tmp_path, source, target, profile)
    with pytest.raises(restore.Blocked, match="Backup directory"):
        restore.check_paths(tmp_path, tmp_path, target, profile)
    target.mkdir()
    with pytest.raises(restore.Blocked, match="already exists"):
        restore.check_paths(tmp_path, source, target, profile)


def test_port_conflict(monkeypatch):
    monkeypatch.setattr(restore.socket, "SO_EXCLUSIVEADDRUSE", 1, raising=False)
    listener = MagicMock()
    listener.__enter__.return_value = listener
    listener.bind.side_effect = OSError
    monkeypatch.setattr(restore.socket, "socket", Mock(return_value=listener))
    with pytest.raises(restore.Blocked, match="58530"):
        restore.check_port()
    listener.bind.assert_called_once_with(("127.0.0.1", 58530))


def test_versions_fail_on_mixed_tools():
    run = Mock(side_effect=lambda name, _: (
        f"{name} (PostgreSQL) {'17.12' if name == 'pg_dump' else '18.4'}".encode()
    ))
    with pytest.raises(restore.Blocked, match="pg_dump"):
        restore.check_versions(run)


def manifest_for(profile):
    manifest = {
        "timestamp_utc": "2026-11-23T12:00:00+00:00",
        "project_reference": "pxiwoowpoewxvnsjwfdw", "postgresql_version": "17.11",
        "backup_tool_version": "18.4", "alembic_revision": profile.revision.decode(),
        "archive": "development.dump", "archive_size_bytes": profile.size,
        "sha256": profile.sha256, "status": "ARCHIVE CREATED AND VERIFIED",
        "restore_verified": False, "backup_scope": restore.backup.SCOPE,
    }
    if profile is restore.CURRENT:
        manifest["inventory"] = {
            "tables": sorted(profile.tables), "sequences": sorted(profile.sequences),
        }
    return manifest


def write_fixture_manifest(path, profile, manifest):
    contents = json.dumps(manifest).encode()
    if profile.legacy_manifest_size:
        contents = contents.ljust(profile.legacy_manifest_size)
    path.write_bytes(contents)


@pytest.mark.parametrize("profile", [restore.LEGACY, restore.CURRENT])
def test_manifest_and_archive_pins(tmp_path, monkeypatch, profile):
    manifest = manifest_for(profile)
    manifest_path = tmp_path / "manifest.json"
    archive = tmp_path / restore.ARCHIVE
    archive.write_bytes(b"x" * profile.size)
    checksum = Mock(return_value=profile.sha256)
    monkeypatch.setattr(restore.backup, "archive_checksum", checksum)
    write_fixture_manifest(manifest_path, profile, manifest)
    restore.check_manifest(tmp_path, profile)
    checksum.assert_called_once_with(archive)

    checksum.return_value = "0" * 64
    with pytest.raises(restore.Blocked, match="SHA-256"):
        restore.check_manifest(tmp_path, profile)
    checksum.reset_mock()
    archive.write_bytes(b"x" * (profile.size - 1))
    with pytest.raises(restore.Blocked, match="size"):
        restore.check_manifest(tmp_path, profile)
    checksum.assert_not_called()

    manifest["alembic_revision"] = "83ca40c9f279"
    write_fixture_manifest(manifest_path, profile, manifest)
    with pytest.raises(restore.Blocked, match="Manifest"):
        restore.check_manifest(tmp_path, profile)


@pytest.mark.parametrize(("field", "value"), [
    ("project_reference", "wrong-project"),
    ("postgresql_version", "18.4"),
    ("backup_tool_version", "17.11"),
    ("archive", "synthetic-valid.dump"),
    ("archive_size_bytes", 51811.0),
    ("archive_size_bytes", 46382),
    ("sha256", "0" * 64),
    ("restore_verified", True),
    ("status", "PENDING"),
    ("timestamp_utc", "2026-11-23T12:00:00"),
    ("unknown_field", True),
    ("inventory", {"tables": [], "sequences": []}),
])
def test_current_manifest_rejects_contradictions(tmp_path, monkeypatch, field, value):
    profile = restore.CURRENT
    checksum = Mock()
    monkeypatch.setattr(restore.backup, "archive_checksum", checksum)
    manifest = manifest_for(profile)
    manifest[field] = value
    write_fixture_manifest(tmp_path / "manifest.json", profile, manifest)
    with pytest.raises(restore.Blocked, match="Manifest"):
        restore.check_manifest(tmp_path, profile)
    checksum.assert_not_called()


@pytest.mark.parametrize("entry", ["tables", "sequences"])
def test_current_manifest_rejects_unexpected_inventory(tmp_path, entry):
    profile = restore.CURRENT
    manifest = manifest_for(profile)
    manifest["inventory"][entry].append("unexpected")
    write_fixture_manifest(tmp_path / "manifest.json", profile, manifest)
    with pytest.raises(restore.Blocked, match="inventory"):
        restore.check_manifest(tmp_path, profile)


@pytest.mark.parametrize("field", ["project_reference", "inventory", "restore_verified", "timestamp_utc"])
def test_current_manifest_rejects_missing_fields(tmp_path, field):
    profile = restore.CURRENT
    manifest = manifest_for(profile)
    del manifest[field]
    write_fixture_manifest(tmp_path / "manifest.json", profile, manifest)
    with pytest.raises(restore.Blocked, match="Manifest"):
        restore.check_manifest(tmp_path, profile)


def test_manifest_rejects_malformed_json_and_duplicate_fields(tmp_path):
    path = tmp_path / "manifest.json"
    for data in (b"{", b'{"archive":"wrong","archive":"development.dump"}'):
        path.write_bytes(data)
        with pytest.raises(restore.Blocked, match="Manifest"):
            restore.check_manifest(tmp_path, restore.CURRENT)


def test_current_profile_is_explicit_and_pinned():
    profile = restore.selected_profile(["--profile", "d2b7a4e9310c"])
    assert profile is restore.CURRENT
    assert profile.source == "Learn2Play-Development-pre-migration-2026-11-23"
    assert profile.target != restore.LEGACY.target
    assert profile.size == 51811
    assert profile.sha256 == "693fed196119c0b8d1723f29a652c91f2631e6e0335ebb190bdcc0505dce6a79"
    assert len(profile.tables) == 10 and "xp_events" in profile.tables
    assert len(profile.sequences) == 9 and "xp_events_id_seq" in profile.sequences
    assert restore.selected_profile(["--profile", "9e45f7e7aa32"]) is restore.LEGACY
    for arguments in ([], ["--profile", "unknown"], ["--profile", "9e45f7e7aa32", "d2b7a4e9310c"]):
        with pytest.raises(restore.Blocked, match="Select exactly one"):
            restore.selected_profile(arguments)


@pytest.mark.parametrize("selected", [restore.LEGACY, restore.CURRENT])
def test_inventory_and_comparison(selected):
    profile = replace(selected, tables=frozenset({"alembic_version", "users"}),
                      sequences=frozenset({"users_id_seq"}))
    sql = br"""
-- Name: users; Type: TABLE; Schema: public; Owner: -
CREATE TABLE public.users (id integer);
-- Name: alembic_version; Type: TABLE; Schema: public; Owner: -
CREATE TABLE public.alembic_version (version_num varchar);
-- Name: users_id_seq; Type: SEQUENCE; Schema: public; Owner: -
CREATE SEQUENCE public.users_id_seq;
-- Data for Name: users; Type: TABLE DATA; Schema: public; Owner: -
COPY public.users (id) FROM stdin;
1
2
\.
-- Data for Name: alembic_version; Type: TABLE DATA; Schema: public; Owner: -
COPY public.alembic_version (version_num) FROM stdin;
9e45f7e7aa32
\.
-- Name: users_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
SELECT pg_catalog.setval('public.users_id_seq', 2, true);
"""
    sql = sql.replace(b"9e45f7e7aa32", profile.revision)
    expected = restore.summarize_dump(sql, profile)
    restore.verify_restore(expected, restore.summarize_dump(sql.replace(b"\n1\n2\n", b"\n2\n1\n"), profile))
    for old, new, label in (
        (b"id integer", b"id bigint", "schema definitions"),
        (b"\n1\n2\n", b"\n1\n3\n", "table rows"),
        (b"2, true", b"2, false", "sequence states"),
    ):
        with pytest.raises(restore.Blocked, match=label):
            restore.verify_restore(expected, restore.summarize_dump(sql.replace(old, new), profile))
    with pytest.raises(restore.Blocked, match="inventory"):
        restore.summarize_dump(sql.replace(b"users_id_seq", b"unexpected_id_seq"), profile)
    with pytest.raises(restore.Blocked, match="revision"):
        restore.summarize_dump(sql.replace(profile.revision, b"83ca40c9f279"), profile)


XP_ARCHIVE_CHECK = (
    b"    CONSTRAINT ck_xp_events_path CHECK (((path)::text = ANY "
    b"((ARRAY['academic'::character varying, 'gaming'::character varying])::text[]))),"
)
XP_RESTORED_CHECK = (
    b"    CONSTRAINT ck_xp_events_path CHECK (((path)::text = ANY "
    b"(ARRAY[('academic'::character varying)::text, ('gaming'::character varying)::text]))),"
)


@pytest.fixture
def xp_schema_pair():
    key = (b"TABLE", b"xp_events")
    table = b"CREATE TABLE public.xp_events (\n    path character varying(20) NOT NULL,\n%s\n    id integer\n);"
    return key, ({key: table % XP_ARCHIVE_CHECK}, {}, {}), ({key: table % XP_RESTORED_CHECK}, {}, {})


def test_xp_constraint_accepts_verified_postgresql_cast_rewrite(xp_schema_pair):
    key, archive, restored = xp_schema_pair
    assert archive[0] != restored[0]
    restore.verify_restore(archive, restored)
    assert XP_RESTORED_CHECK in restored[0][key]


@pytest.mark.parametrize(("old", "new"), [
    (b"'gaming'", b"'other'"),
    (b"= ANY", b"<> ANY"),
    (b"(path)::text", b"(other_path)::text"),
    (b"ck_xp_events_path", b"ck_other_path"),
    (b"character varying(20)", b"character varying(30)"),
    (b"id integer", b"id bigint"),
])
def test_xp_constraint_rejects_other_schema_changes(xp_schema_pair, old, new):
    key, archive, restored = xp_schema_pair
    restored[0][key] = restored[0][key].replace(old, new)
    with pytest.raises(restore.Blocked, match="schema definitions"):
        restore.verify_restore(archive, restored)


def test_xp_constraint_cast_exception_does_not_apply_to_another_table(xp_schema_pair):
    key, archive, restored = xp_schema_pair
    other = (b"TABLE", b"other_events")
    for schema, _, _ in (archive, restored):
        schema[other] = schema.pop(key).replace(b"public.xp_events", b"public.other_events")
    with pytest.raises(restore.Blocked, match="schema definitions"):
        restore.verify_restore(archive, restored)


@pytest.mark.parametrize("profile", [restore.LEGACY, restore.CURRENT])
@pytest.mark.parametrize("outcome", ["success", "wrong_port", "startup_failed", "changed_cluster"])
def test_restore_checks_identity_before_writing(tmp_path, monkeypatch, outcome, profile):
    target = tmp_path / profile.target
    data = target / "data"
    source = tmp_path / profile.source
    source.mkdir()
    (source / restore.ARCHIVE).touch()
    for name in ("check_paths", "check_manifest", "check_versions", "check_archive", "check_port"):
        monkeypatch.setattr(restore, name, Mock())
    monkeypatch.setattr(restore.backup, "create_restricted_directory", Path.mkdir)
    monkeypatch.setattr(restore, "restore_list", Mock(return_value=b""))
    summary = ({}, {}, {})
    monkeypatch.setattr(restore, "summarize_dump", Mock(return_value=summary))
    identifiers = [b"12345", b"changed" if outcome == "changed_cluster" else b"12345"]
    monkeypatch.setattr(restore, "system_identifier", Mock(side_effect=identifiers))

    def command(name, *args, **kwargs):
        if name == "initdb":
            assert "-A" in args and "scram-sha-256" in args and restore.ROLE in args
            data.mkdir()
        if name == "pg_ctl" and "start" in args:
            assert "-h 127.0.0.1 -p 58530" in args[-1]
            assert "-w" in args and kwargs == {"startup_log": target / "pg_ctl-start.log"}
            (data / "postmaster.pid").touch()
            if outcome in {"startup_failed", "changed_cluster"}:
                raise restore.Blocked("Scratch startup: launcher failed.")
        if name == "psql":
            port = 5432 if outcome == "wrong_port" else 58530
            assert "-W" in args and "hostaddr=127.0.0.1 port=58530" in args[-3]
            assert "BEGIN READ ONLY" in args[-1]
            return json.dumps(["postgres", restore.ROLE, "127.0.0.1", port, "127.0.0.1",
                               "180004", str(data), "12345", "on"]).encode()
        return b""

    run = Mock(side_effect=command)
    if outcome == "success":
        restore.restore(tmp_path, run, profile)
        assert [call.args[0] for call in run.call_args_list][-4:] == [
            "pg_ctl", "psql", "pg_restore", "pg_dump",
        ]
        restore_call = run.call_args_list[-2].args
        assert {"--single-transaction", "--no-acl", "--no-owner", "--exit-on-error", "-W"} <= set(restore_call)
        assert "--clean" not in restore_call
        assert restore.CONNECTION in restore_call
    else:
        message = "Server identity" if outcome == "wrong_port" else "Scratch startup"
        with pytest.raises(restore.Blocked, match=message):
            restore.restore(tmp_path, run, profile)
        shutdown = ("pg_ctl", "-D", data, "-w", "-m", "fast", "stop")
        assert (run.call_args.args == shutdown) == (outcome != "changed_cluster")
        if outcome != "wrong_port":
            assert not any(call.args[0] == "psql" for call in run.call_args_list)
        assert not any("--dbname" in call.args for call in run.call_args_list)
    assert (source / restore.ARCHIVE).exists()


def test_restore_list_skips_only_public_creation():
    toc = (b"; Format: CUSTOM\n; Dumped from database version: 17.11\n"
           b"; Dumped by pg_dump version: 18.4\n"
           b"38; 2615 2200 SCHEMA - public pg_database_owner\n"
           b"41; 0 0 COMMENT - SCHEMA public pg_database_owner\n")
    assert restore.restore_list(toc) == toc.replace(b"38;", b";38;")
    with pytest.raises(restore.Blocked, match="CREATE SCHEMA"):
        restore.restore_list(toc.replace(b"SCHEMA - public", b"SCHEMA - other"))


def test_tool_runner_discards_ambient_remote_connection_settings(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "unused-fixture")
    monkeypatch.setenv("PGHOST", "unused-fixture")
    monkeypatch.setenv("PGSERVICE", "unused-fixture")
    monkeypatch.setattr(Path, "is_file", lambda _: True)
    run_tool = Mock(return_value=b"")
    monkeypatch.setattr(restore.backup, "run_tool", run_tool)
    restore.tool_runner()("psql", "-d", restore.CONNECTION)
    _, args, env = run_tool.call_args.args
    assert not any(key.upper().startswith("PG") or key.upper() == "DATABASE_URL" for key in env)
    assert args[1].startswith("host=127.0.0.1 hostaddr=127.0.0.1 port=58530 ")


@pytest.mark.parametrize(("tool", "prompts"), [
    ("initdb", (b"Enter new superuser password: ", b"Enter it again: ")),
    ("psql", (b"Password: ",)),
    ("pg_restore", (b"Password: ",)),
    ("pg_dump", (b"Password: ",)),
])
def test_tool_runner_enables_prompt_relay_only_for_password_calls(monkeypatch, tool, prompts):
    monkeypatch.setattr(Path, "is_file", lambda _: True)
    run_tool = Mock(return_value=b"")
    monkeypatch.setattr(restore.backup, "run_tool", run_tool)
    run = restore.tool_runner()
    run(tool, "--version")
    assert run_tool.call_args.kwargs == {}
    run(tool, "-W")
    assert run_tool.call_args.kwargs == {"password_prompts": prompts}


@pytest.mark.parametrize("returncode", [0, 1])
def test_password_tool_relays_partial_prompts_and_sanitizes_errors(monkeypatch, capsys, returncode):
    from contextlib import contextmanager

    backup = restore.backup
    events = []

    @contextmanager
    def hidden_console():
        events.append("echo disabled")
        try:
            yield
        finally:
            events.append("echo restored")

    def stderr_chunks():
        assert events == ["echo disabled"]
        yield b"Enter new super"
        assert capsys.readouterr().err == ""
        yield b"user password: "
        assert capsys.readouterr().err == "Enter new superuser password: "
        yield b"\r\nEnter it "
        assert capsys.readouterr().err == "\n"
        yield b"again: "
        assert capsys.readouterr().err == "Enter it again: "
        yield b"\r\npermission denied: private fixture detail\n"
        yield b""

    process = MagicMock()
    process.stdout.read.return_value = b"result"
    process.stderr.read1.side_effect = stderr_chunks()
    process.wait.return_value = returncode
    popen = MagicMock()
    popen.return_value.__enter__.return_value = process
    monkeypatch.setattr(backup, "hidden_windows_console", hidden_console)
    monkeypatch.setattr(backup.subprocess, "Popen", popen)

    def run():
        return backup.run_tool(Path("initdb"), ["-W"], password_prompts=restore.PASSWORD_PROMPTS["initdb"])

    if returncode:
        with pytest.raises(backup.catalog.Blocked, match="The tool reported a permission error") as failure:
            run()
        assert "private fixture detail" not in str(failure.value)
    else:
        assert run() == b"result"
    assert events == ["echo disabled", "echo restored"]
    assert capsys.readouterr().err == "\n"
    assert "stdin" not in popen.call_args.kwargs
    assert popen.call_args.kwargs["stderr"] == backup.subprocess.PIPE


def test_noninteractive_tool_keeps_stderr_private(monkeypatch, capsys):
    backup = restore.backup
    subprocess_run = Mock(return_value=backup.subprocess.CompletedProcess(
        ["pg_restore", "--list"], 1, b"", b"permission denied: private fixture detail"
    ))
    interactive = Mock()
    monkeypatch.setattr(backup.subprocess, "run", subprocess_run)
    monkeypatch.setattr(backup, "run_password_tool", interactive)
    with pytest.raises(backup.catalog.Blocked, match="The tool reported a permission error") as failure:
        backup.run_tool(Path("pg_restore"), ["--list"])
    assert "private fixture detail" not in str(failure.value)
    assert capsys.readouterr().err == ""
    assert subprocess_run.call_args.kwargs["stderr"] == backup.subprocess.PIPE
    interactive.assert_not_called()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows handle inheritance")
@pytest.mark.parametrize("capture", [True, False], ids=["pipes", "file"])
def test_windows_startup_descendant_handles(tmp_path, capture):
    import _winapi
    import subprocess

    ready = tmp_path / "ready"
    release = tmp_path / "release"
    descendant = """
import os, sys, time
from pathlib import Path
ready, release = map(Path, sys.argv[1:])
ready.write_text(str(os.getpid()))
deadline = time.monotonic() + 8
while not release.exists() and time.monotonic() < deadline:
    time.sleep(0.02)
"""
    parent = """
import subprocess, sys, time
from pathlib import Path
exe, child, ready, release = sys.argv[1:]
print("startup stdout", flush=True)
print("startup stderr", file=sys.stderr, flush=True)
subprocess.Popen([exe, "-I", "-B", "-c", child, ready, release], close_fds=False)
deadline = time.monotonic() + 3
while not Path(ready).exists() and time.monotonic() < deadline:
    time.sleep(0.02)
"""
    executable = sys._base_executable
    command = [executable, "-I", "-B", "-c", parent, executable, descendant, str(ready), str(release)]
    log = tmp_path / "startup.log"
    process = None
    handle = None
    try:
        if capture:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            assert process.wait(timeout=5) == 0
        else:
            restore.run_startup(command, None, log)
        handle = _winapi.OpenProcess(0x00100001, False, int(ready.read_text()))
        assert _winapi.WaitForSingleObject(handle, 0) == _winapi.WAIT_TIMEOUT
        if capture:
            with pytest.raises(subprocess.TimeoutExpired):
                process.communicate(timeout=0.25)
        else:
            assert log.read_bytes().strip() == b"startup stderr"
    finally:
        release.touch()
        try:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
            if handle is None and ready.is_file():
                try:
                    handle = _winapi.OpenProcess(0x00100001, False, int(ready.read_text()))
                except OSError as error:
                    if error.winerror != 87:  # The fixture descendant already exited.
                        raise
            if handle is not None and _winapi.WaitForSingleObject(handle, 3000) == _winapi.WAIT_TIMEOUT:
                _winapi.TerminateProcess(handle, 1)
                assert _winapi.WaitForSingleObject(handle, 3000) == _winapi.WAIT_OBJECT_0
        finally:
            if handle is not None:
                _winapi.CloseHandle(handle)
            if process is not None:
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()


@pytest.mark.parametrize(("failure", "message"), [
    (KeyboardInterrupt(), "canceled"),
    (OSError("private detail"), "I/O failed"),
    (restore.subprocess.TimeoutExpired(["private argument"], 90), "timed out"),
])
def test_startup_errors_are_stage_specific_and_redacted(tmp_path, monkeypatch, failure, message):
    monkeypatch.setattr(restore.subprocess, "run", Mock(side_effect=failure))
    with pytest.raises(restore.Blocked, match=f"Scratch startup: .*{message}") as error:
        restore.run_startup(["private argument"], None, tmp_path / "startup.log")
    assert "private" not in str(error.value)


def test_interrupted_password_tool_kills_child_and_restores_console(monkeypatch, capsys):
    from contextlib import contextmanager

    backup = restore.backup
    events = []

    @contextmanager
    def hidden_console():
        events.append("echo disabled")
        try:
            yield
        finally:
            events.append("echo restored")

    process = MagicMock()
    process.poll.return_value = None
    process.stderr.read1.side_effect = KeyboardInterrupt
    process.kill.side_effect = lambda: events.append("child killed")
    popen = MagicMock()
    popen.return_value.__enter__.return_value = process
    monkeypatch.setattr(backup, "hidden_windows_console", hidden_console)
    monkeypatch.setattr(backup.subprocess, "Popen", popen)

    with pytest.raises(KeyboardInterrupt):
        backup.run_tool(Path("psql"), ["-W"], password_prompts=restore.PASSWORD_PROMPTS["psql"])

    assert events == ["echo disabled", "child killed", "echo restored"]
    assert capsys.readouterr() == ("", "")
