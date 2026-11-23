"""Restore the pinned Development archive to a new Windows loopback-only cluster."""
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import supabase_development_backup as backup

ARCHIVE = "development.dump"
PORT = 58530
ROLE = "l2p_dev_restore"


@dataclass(frozen=True)
class BackupProfile:
    revision: bytes
    source: str
    target: str
    size: int
    sha256: str
    tables: frozenset[str]
    sequences: frozenset[str]
    legacy_manifest_size: int | None = None


LEGACY = BackupProfile(
    revision=b"9e45f7e7aa32",
    source="Learn2Play-Development-dev-before-migrations-2026-11-22-retry-2",
    target="Learn2Play-Development-Restore-58530",
    size=46382,
    sha256="4f03b070a4bf45c71b4ed5ee6849d22452b10c1c14140131bc0d773ed549b866",
    tables=frozenset(backup.TABLES),
    sequences=frozenset(backup.BASELINE_SEQUENCES),
    legacy_manifest_size=870,
)
CURRENT = BackupProfile(
    revision=b"d2b7a4e9310c",
    source="Learn2Play-Development-pre-migration-2026-11-23",
    target="Learn2Play-Development-Restore-58530-d2b7a4e9310c",
    size=51811,
    sha256="693fed196119c0b8d1723f29a652c91f2631e6e0335ebb190bdcc0505dce6a79",
    tables=LEGACY.tables | {"xp_events"},
    sequences=LEGACY.sequences | {"xp_events_id_seq"},
)
PROFILES = {profile.revision.decode(): profile for profile in (LEGACY, CURRENT)}
TOOLS = ("initdb", "pg_ctl", "pg_controldata", "postgres", "pg_restore", "pg_dump", "psql")
CONNECTION = (
    f"host=127.0.0.1 hostaddr=127.0.0.1 port={PORT} dbname=postgres user={ROLE} "
    "sslmode=disable gssencmode=disable require_auth=scram-sha-256 connect_timeout=5"
)
HEADER = re.compile(rb"^-- (?:Data for )?Name: ([^;]+); Type: ([^;]+); Schema: ([^;]+); Owner: .*$", re.M)
COPY = re.compile(rb"COPY public\.([a-z_]+) \(([^)]+)\) FROM stdin;")
SETVAL = re.compile(rb"SELECT pg_catalog\.setval\('public\.([a-z_]+)', (-?\d+), (true|false)\);")


class Blocked(Exception):
    pass


def require(condition, message):
    if not condition:
        raise Blocked(message)


def check_paths(local, source, target, profile):
    require(local.is_absolute() and local.is_dir() and local.resolve(strict=True) == local,
            "LOCALAPPDATA must be an existing canonical directory.")
    require(source == local / profile.source and source.is_dir() and source.resolve(strict=True) == source,
            "Backup directory is missing or redirected.")
    for name in (ARCHIVE, "manifest.json"):
        path = source / name
        require(path.is_file() and path.resolve(strict=True) == path and not path.is_symlink(),
                "A required backup file is missing or redirected.")
    require(target == local / profile.target and not os.path.lexists(target),
            "Restore directory or data directory already exists, or the target is invalid.")


def check_archive(path, profile):
    require(path.name == ARCHIVE and path.stat().st_size == profile.size
            and backup.archive_checksum(path) == profile.sha256,
            "Archive filename, size, or pinned SHA-256 does not match.")


def unique_manifest_fields(pairs):
    result = dict(pairs)
    require(len(result) == len(pairs), "Manifest contains duplicate fields.")
    return result


def check_manifest(source, profile):
    path = source / "manifest.json"
    size = path.stat().st_size
    require(size == profile.legacy_manifest_size if profile.legacy_manifest_size is not None
            else 0 < size <= 4096, "Unexpected manifest size.")
    try:
        manifest = json.loads(path.read_bytes(), object_pairs_hook=unique_manifest_fields)
    except (ValueError, UnicodeError):
        raise Blocked("Manifest is malformed.") from None
    expected = {
        "project_reference": "pxiwoowpoewxvnsjwfdw",
        "postgresql_version": "17.11", "backup_tool_version": "18.4",
        "alembic_revision": profile.revision.decode(), "archive": ARCHIVE,
        "archive_size_bytes": profile.size, "sha256": profile.sha256,
        "status": "ARCHIVE CREATED AND VERIFIED",
    }
    require(isinstance(manifest, dict)
            and all(manifest.get(key) == value for key, value in expected.items())
            and type(manifest["archive_size_bytes"]) is int
            and manifest.get("restore_verified") is False
            and isinstance(manifest.get("backup_scope"), dict)
            and manifest["backup_scope"].get("included") == backup.SCOPE["included"],
            "Manifest does not describe the pinned Development backup.")
    if profile.legacy_manifest_size is None:
        require(set(manifest) == expected.keys() | {"timestamp_utc", "inventory", "backup_scope", "restore_verified"}
                and manifest["backup_scope"] == backup.SCOPE
                and manifest["inventory"] == {
                    "tables": sorted(profile.tables), "sequences": sorted(profile.sequences),
                }, "Manifest scope or inventory does not match the selected Development backup.")
        try:
            timestamp = datetime.fromisoformat(manifest["timestamp_utc"])
        except (TypeError, ValueError):
            raise Blocked("Manifest timestamp is invalid.") from None
        require(timestamp.utcoffset() == timedelta(0), "Manifest timestamp must use UTC.")
    check_archive(source / ARCHIVE, profile)


def check_port():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind(("127.0.0.1", PORT))
    except OSError:
        raise Blocked(f"Cannot exclusively bind local port {PORT}.") from None


def tool_runner():
    # Prevent libpq defaults, service settings, credentials or logging overrides
    # from leaking in from the caller. Use explicit connection parameters.
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith("PG") and key.upper() != "DATABASE_URL"}
    env["LC_ALL"] = "C"

    def run(name, *arguments):
        tool = backup.BIN / f"{name}.exe"
        require(tool.is_file(), f"Required PostgreSQL tool is missing: {name}.")
        return backup.run_tool(tool, [str(argument) for argument in arguments], env)

    return run


def check_versions(run):
    for name in TOOLS:
        output = run(name, "--version").strip()
        require(re.fullmatch(rb"[a-z_]+ \(PostgreSQL\) 18\.4", output),
                f"Expected PostgreSQL 18.4 for {name}.")


def restore_list(listing):
    rows = listing.splitlines()
    for expected in (b"Format: CUSTOM", b"Dumped from database version: 17.11",
                     b"Dumped by pg_dump version: 18.4"):
        require(any(line.lstrip(b"; ") == expected for line in rows),
                "Unexpected archive format or version metadata.")
    entries = [i for i, line in enumerate(rows)
               if re.fullmatch(rb"\d+; 2615 2200 SCHEMA - public pg_database_owner", line)]
    require(len(entries) == 1, "Expected exactly one archive CREATE SCHEMA public entry.")
    # initdb already creates public. Retain the archived comment, skip ACLs at restore.
    i = entries[0]
    rows[i] = b";" + rows[i]
    return b"\n".join(rows) + b"\n"


def summarize_dump(sql, profile):
    """Return exact schema sections, unordered row fingerprints and sequence states."""
    sql = sql.replace(b"\r\n", b"\n")
    headers = list(HEADER.finditer(sql))
    schema, data, sequences = {}, {}, {}
    for i, header in enumerate(headers):
        name, kind, namespace = header.groups()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(sql)
        section = sql[header.end():end].splitlines()
        lines = [line for line in section
                 if line and not line.startswith((b"--", b"\\unrestrict "))]
        if (namespace == b"-" and ((kind == b"SCHEMA" and name == b"public")
                                   or (kind == b"COMMENT" and name == b"SCHEMA public"))):
            continue
        require(namespace == b"public" and lines, "Unexpected dump section.")
        if kind == b"TABLE DATA":
            copy = COPY.fullmatch(lines[0])
            require(copy and copy[1] == name and len(lines) >= 2 and lines[-1] == b"\\.",
                    "Unsupported archive COPY format.")
            require(name not in data, "Duplicate table data section.")
            start = section.index(lines[0]) + 1
            finish = section.index(b"\\.", start)
            rows = section[start:finish]
            if name == b"alembic_version":
                require(copy[2] == b"version_num" and rows == [profile.revision],
                        "Alembic revision is not the reviewed baseline.")
            data[name] = (copy[2], len(rows), sorted(hashlib.sha256(row).digest() for row in rows))
        elif kind == b"SEQUENCE SET":
            state = SETVAL.fullmatch(lines[0])
            require(len(lines) == 1 and state and state[1] == name and name not in sequences,
                    "Unsupported sequence state format.")
            sequences[name] = (int(state[2]), state[3] == b"true")
        else:
            key = (kind, name)
            require(key not in schema, "Duplicate schema definition.")
            schema[key] = b"\n".join(lines)
    require(set(data) == {name.encode() for name in profile.tables}
            and set(sequences) == {name.encode() for name in profile.sequences}
            and {name for kind, name in schema if kind == b"TABLE"} == set(data)
            and {name for kind, name in schema if kind == b"SEQUENCE"} == set(sequences),
            "Dump table or sequence inventory differs from the reviewed baseline.")
    return schema, data, sequences


def verify_restore(expected, actual):
    for old, new, label in zip(expected, actual, ("schema definitions", "table rows", "sequence states")):
        require(old == new, f"Restored {label} differ from the archive.")


def system_identifier(run, data):
    output = run("pg_controldata", data)
    matches = re.findall(rb"(?m)^Database system identifier:[ \t]*(\d+)\s*$", output)
    require(len(matches) == 1, "Cannot establish the scratch cluster system identifier.")
    return matches[0]


def verify_identity(run, data, cluster_id):
    query = """
BEGIN READ ONLY;
SELECT json_build_array(current_database(), session_user,
 host(inet_server_addr()), inet_server_port(), current_setting('listen_addresses'),
 current_setting('server_version_num'), current_setting('data_directory'),
 (pg_control_system()).system_identifier::text, current_setting('transaction_read_only'));
ROLLBACK;
"""
    row = json.loads(run("psql", "-X", "-W", "-qAt", "-v", "ON_ERROR_STOP=1",
                         "-d", CONNECTION, "-c", query))
    require(isinstance(row, list) and len(row) == 9
            and row[:6] == ["postgres", ROLE, "127.0.0.1", PORT, "127.0.0.1", "180004"]
            and Path(row[6]).resolve(strict=True) == data
            and row[7:] == [cluster_id.decode(), "on"],
            "Server identity does not match the new loopback scratch cluster.")


def restore(local, run, profile):
    source, target = local / profile.source, local / profile.target
    data = target / "data"
    check_paths(local, source, target, profile)
    check_manifest(source, profile)
    check_versions(run)
    check_port()

    backup.create_restricted_directory(target)
    archive = target / ARCHIVE
    with (source / ARCHIVE).open("rb") as original, archive.open("xb") as copied:
        shutil.copyfileobj(original, copied)
    check_archive(archive, profile)
    toc = target / "restore.list"
    with toc.open("xb") as handle:
        handle.write(restore_list(run("pg_restore", "--list", archive)))
    options = ("--no-owner", "--no-acl")
    expected = summarize_dump(run("pg_restore", *options, "--file=-", archive), profile)
    tool_path = str(backup.BIN / "pg_ctl.exe").replace("'", "''")
    data_path = str(data).replace("'", "''")
    shutdown = f"& '{tool_path}' -D '{data_path}' -w -m fast stop"

    print("Choose a strong, unique scratch password. PostgreSQL will prompt again for it.", flush=True)
    run("initdb", "-D", data, "-U", ROLE, "-A", "scram-sha-256",
        "--encoding=UTF8", "--no-locale", "-W")
    cluster_id = system_identifier(run, data)
    check_port()
    try:
        run("pg_ctl", "-D", data, "-l", target / "postgres.log", "-w", "start", "-o",
            f"-h 127.0.0.1 -p {PORT} -c log_min_error_statement=panic")
        print("Enter the scratch password for identity verification, restore, and comparison.", flush=True)
        verify_identity(run, data, cluster_id)
        run("pg_restore", *options, "--use-list", toc, "--single-transaction", "--exit-on-error",
            "-W", "--dbname", CONNECTION, archive)
        actual = summarize_dump(run("pg_dump", *options, "--schema=public", "--format=plain",
                                    "-W", "--dbname", CONNECTION), profile)
        verify_restore(expected, actual)
    except BaseException:
        # pg_ctl consults the new cluster's PID file, never a Windows service.
        if (data / "postmaster.pid").is_file():
            try:
                require(system_identifier(run, data) == cluster_id, "Scratch cluster identity changed.")
                run("pg_ctl", "-D", data, "-w", "-m", "fast", "stop")
            except (Exception, KeyboardInterrupt):
                print("Automatic scratch shutdown could not be confirmed. Inspect the scratch instance.", file=sys.stderr)
        print("Scratch files were retained. Inspect them before retrying.", file=sys.stderr)
        raise

    print(f"PASS: {len(actual[0])} schema definitions, {len(actual[1])} tables, "
          f"{len(actual[2])} sequences, and Alembic revision {profile.revision.decode()}.")
    for name, (_, count, _) in sorted(actual[1].items()):
        print(f"  {name.decode()}: {count} rows")
    print(f"Scratch server: 127.0.0.1:{PORT}, database postgres, login {ROLE}")
    print(f"Stop the scratch server with:\n{shutdown}")


def selected_profile(arguments):
    if len(arguments) != 2 or arguments[0] != "--profile" or arguments[1] not in PROFILES:
        raise Blocked("Select exactly one pinned backup using --profile 9e45f7e7aa32 or --profile d2b7a4e9310c.")
    return PROFILES[arguments[1]]


def main():
    try:
        profile = selected_profile(sys.argv[1:])
        require(os.name == "nt" and os.environ.get("LOCALAPPDATA"),
                "Run this helper from an interactive Windows PowerShell console.")
        with backup.hidden_windows_console():
            restore(Path(os.environ["LOCALAPPDATA"]), tool_runner(), profile)
    except (Blocked, backup.catalog.Blocked) as exc:
        print(f"RESTORE STOPPED: {exc}", file=sys.stderr)
        return 1
    except (Exception, KeyboardInterrupt):
        print("RESTORE STOPPED: Unexpected local verification, I/O, or console failure.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
