import json
from pathlib import Path
import sys
from unittest.mock import MagicMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import restore_supabase_development_local as restore


def test_paths_reject_existing_target_and_wrong_backup(tmp_path):
    source, target = tmp_path / restore.SOURCE, tmp_path / restore.TARGET
    source.mkdir()
    for name in (restore.ARCHIVE, "manifest.json"):
        (source / name).touch()
    restore.check_paths(tmp_path, source, target)
    with pytest.raises(restore.Blocked, match="Backup directory"):
        restore.check_paths(tmp_path, tmp_path, target)
    target.mkdir()
    with pytest.raises(restore.Blocked, match="already exists"):
        restore.check_paths(tmp_path, source, target)


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


def test_manifest_and_archive_pins(tmp_path, monkeypatch):
    manifest = {
        "project_reference": "pxiwoowpoewxvnsjwfdw", "postgresql_version": "17.11",
        "backup_tool_version": "18.4", "alembic_revision": "9e45f7e7aa32",
        "archive": "development.dump", "archive_size_bytes": 46382,
        "sha256": restore.SHA256, "status": "ARCHIVE CREATED AND VERIFIED",
        "restore_verified": False,
        "backup_scope": {"included": "public schema and all approved baseline table data, including public.alembic_version"},
    }
    manifest_path = tmp_path / "manifest.json"
    archive = tmp_path / restore.ARCHIVE
    archive.write_bytes(b"x" * 46382)
    checksum = Mock(return_value=restore.SHA256)
    monkeypatch.setattr(restore.backup, "archive_checksum", checksum)
    manifest_path.write_bytes(json.dumps(manifest).encode().ljust(870))
    restore.check_manifest(tmp_path)
    checksum.return_value = "0" * 64
    with pytest.raises(restore.Blocked, match="SHA-256"):
        restore.check_manifest(tmp_path)
    manifest["alembic_revision"] = "83ca40c9f279"
    manifest_path.write_bytes(json.dumps(manifest).encode().ljust(870))
    with pytest.raises(restore.Blocked, match="Manifest"):
        restore.check_manifest(tmp_path)


def test_inventory_and_comparison(monkeypatch):
    monkeypatch.setattr(restore.backup, "TABLES", {"alembic_version", "users"})
    monkeypatch.setattr(restore.backup, "BASELINE_SEQUENCES", {"users_id_seq"})
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
    expected = restore.summarize_dump(sql)
    restore.verify_restore(expected, restore.summarize_dump(sql.replace(b"\n1\n2\n", b"\n2\n1\n")))
    for old, new, label in (
        (b"id integer", b"id bigint", "schema definitions"),
        (b"\n1\n2\n", b"\n1\n3\n", "table rows"),
        (b"2, true", b"2, false", "sequence states"),
    ):
        with pytest.raises(restore.Blocked, match=label):
            restore.verify_restore(expected, restore.summarize_dump(sql.replace(old, new)))
    with pytest.raises(restore.Blocked, match="revision"):
        restore.summarize_dump(sql.replace(restore.REVISION, b"83ca40c9f279"))


@pytest.mark.parametrize("port", [5432, 58530])
def test_restore_checks_identity_before_writing(tmp_path, monkeypatch, port):
    target = tmp_path / restore.TARGET
    data = target / "data"
    source = tmp_path / restore.SOURCE
    source.mkdir()
    (source / restore.ARCHIVE).touch()
    for name in ("check_paths", "check_manifest", "check_versions", "check_archive", "check_port"):
        monkeypatch.setattr(restore, name, Mock())
    monkeypatch.setattr(restore.backup, "create_restricted_directory", Path.mkdir)
    monkeypatch.setattr(restore, "restore_list", Mock(return_value=b""))
    summary = ({}, {}, {})
    monkeypatch.setattr(restore, "summarize_dump", Mock(return_value=summary))
    monkeypatch.setattr(restore, "system_identifier", Mock(return_value=b"12345"))

    def command(name, *args):
        if name == "initdb":
            data.mkdir()
        if name == "pg_ctl" and "start" in args:
            (data / "postmaster.pid").touch()
        if name == "psql":
            assert "-W" in args and "hostaddr=127.0.0.1 port=58530" in args[-3]
            assert "BEGIN READ ONLY" in args[-1]
            return json.dumps(["postgres", restore.ROLE, "127.0.0.1", port, "127.0.0.1",
                               "180004", str(data), "12345", "on"]).encode()
        return b""

    run = Mock(side_effect=command)
    if port == 58530:
        restore.restore(tmp_path, run)
        assert [call.args[0] for call in run.call_args_list][-4:] == [
            "pg_ctl", "psql", "pg_restore", "pg_dump",
        ]
        restore_call = run.call_args_list[-2].args
        assert "--single-transaction" in restore_call and "--no-acl" in restore_call
        assert restore.CONNECTION in restore_call
    else:
        with pytest.raises(restore.Blocked, match="Server identity"):
            restore.restore(tmp_path, run)
        assert run.call_args.args == ("pg_ctl", "-D", data, "-w", "-m", "fast", "stop")
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

