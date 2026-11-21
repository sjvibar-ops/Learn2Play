"""Create a restricted logical archive of the reviewed Development baseline."""

import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import warnings

sys.path.insert(0, str(Path(__file__).resolve().parent))
import supabase_development_preflight as development

catalog = development.catalog
EXPECTED_VERSION = "170011"
EXPECTED_REVISION = "9e45f7e7aa32"
TOOL_VERSION = "18.4"
BIN = Path("C:/Program Files/PostgreSQL/18/bin")
TABLES = {
    "alembic_version", "appointments", "credentials", "mentor_availability",
    "mentor_unavailable_dates", "messages", "reports", "role_applications", "users",
}
BASELINE_SEQUENCES = {f"{name}_id_seq" for name in TABLES - {"alembic_version"}}
INVENTORY = """
SELECT c.relname AS name, c.relkind AS kind,
 CASE WHEN c.relkind='S' THEN pg_catalog.has_sequence_privilege(c.oid,'SELECT')
      WHEN c.relkind='r' THEN pg_catalog.has_table_privilege(c.oid,'SELECT')
      ELSE false END AS readable,
 CASE WHEN c.relkind='r' THEN pg_catalog.row_security_active(c.oid)
      ELSE false END AS rls_active,
 EXISTS (SELECT 1 FROM pg_catalog.pg_inherits i WHERE i.inhrelid=c.oid OR i.inhparent=c.oid)
 AS inheritance,
 EXISTS (SELECT 1 FROM pg_catalog.pg_depend d
         WHERE d.classid='pg_catalog.pg_class'::pg_catalog.regclass AND d.objid=c.oid
         AND d.refclassid='pg_catalog.pg_extension'::pg_catalog.regclass AND d.deptype='e')
 AS extension_member
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='public' AND c.relkind IN ('r','p','S','f','m')
"""
SCOPE = {
    "included": "public schema and all approved baseline table data, including public.alembic_version",
    "metadata": "Application sequences, constraints, indexes, functions, triggers, owners and ACLs",
    "excluded": [
        "Supabase-managed schemas, Storage bucket metadata and file bytes, and cluster-global roles and memberships",
        "Large objects, publications and subscriptions",
    ],
}


def destination_for(home, repository, name):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
        raise catalog.Blocked("Use a short alphanumeric backup name with optional hyphens or underscores.")
    home = home.resolve(strict=True)
    parent = (home / "AppData" / "Local").resolve(strict=True)
    if not parent.is_relative_to(home) or parent.is_relative_to(repository.resolve()):
        raise catalog.Blocked("Backup directory must be outside Git and inside the local user profile.")
    destination = parent / f"Learn2Play-Development-{name}"
    if os.path.lexists(destination):
        raise catalog.Blocked("Backup destination already exists. Choose a new name.")
    return destination


def verify_directory_owner(path, advapi, kernel):
    get_owner = advapi.GetNamedSecurityInfoW
    get_owner.argtypes = (wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
                         ctypes.POINTER(wintypes.LPVOID), wintypes.LPVOID, wintypes.LPVOID,
                         wintypes.LPVOID, ctypes.POINTER(wintypes.LPVOID))
    get_owner.restype = wintypes.DWORD
    process = kernel.GetCurrentProcess
    process.argtypes = ()
    process.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    open_token = advapi.OpenProcessToken
    open_token.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    open_token.restype = wintypes.BOOL
    token_info = advapi.GetTokenInformation
    token_info.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID,
                           wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    token_info.restype = wintypes.BOOL
    equal_sid = advapi.EqualSid
    equal_sid.argtypes = (wintypes.LPVOID, wintypes.LPVOID)
    equal_sid.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not open_token(process(), 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
        raise catalog.Blocked("Could not confirm the current Windows user.")
    descriptor = wintypes.LPVOID()
    try:
        size = wintypes.DWORD()
        token_info(token, 1, None, 0, ctypes.byref(size))  # TokenUser
        if not size.value:
            raise catalog.Blocked("Could not determine the Windows user SID.")
        user_info = ctypes.create_string_buffer(size.value)
        if not token_info(token, 1, user_info, size, ctypes.byref(size)):
            raise catalog.Blocked("Could not read the Windows user SID.")
        # TOKEN_USER begins with SID_AND_ATTRIBUTES, whose first member is the SID pointer.
        user_sid = ctypes.cast(user_info, ctypes.POINTER(wintypes.LPVOID)).contents
        owner = wintypes.LPVOID()
        if get_owner(str(path), 1, 1, ctypes.byref(owner), None, None, None, ctypes.byref(descriptor)):
            raise catalog.Blocked("Could not read the restricted backup directory owner.")
        if not owner or not user_sid or not equal_sid(owner, user_sid):
            raise catalog.Blocked("The backup directory owner is not the current Windows user. No export attempted.")
    finally:
        if descriptor:
            kernel.LocalFree(descriptor)
        close(token)


def create_restricted_directory(path):
    # Supply a protected DACL at creation so the folder never inherits a broad ACL.
    # Owner Rights and LocalSystem are the only access entries. Child files inherit them.
    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("descriptor", wintypes.LPVOID),
                    ("inherit_handle", wintypes.BOOL)]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    volume_path = kernel.GetVolumePathNameW
    volume_path.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
    volume_path.restype = wintypes.BOOL
    volume_info = kernel.GetVolumeInformationW
    volume_info.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
                            wintypes.LPDWORD, wintypes.LPDWORD, wintypes.LPDWORD,
                            wintypes.LPWSTR, wintypes.DWORD)
    volume_info.restype = wintypes.BOOL
    root = ctypes.create_unicode_buffer(32768)
    flags = wintypes.DWORD()
    if (not volume_path(str(path.parent), root, len(root))
            or not volume_info(root, None, 0, None, None, ctypes.byref(flags), None, 0)
            or not flags.value & 0x00000008):  # FILE_PERSISTENT_ACLS
        raise catalog.Blocked("The backup volume must enforce persistent Windows access controls.")
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID),
                        ctypes.POINTER(wintypes.DWORD))
    convert.restype = wintypes.BOOL
    create = kernel.CreateDirectoryW
    create.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(SecurityAttributes))
    create.restype = wintypes.BOOL
    free = kernel.LocalFree
    free.argtypes = (wintypes.LPVOID,)
    free.restype = wintypes.LPVOID
    descriptor = wintypes.LPVOID()
    if not convert("D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)", 1, ctypes.byref(descriptor), None):
        raise catalog.Blocked("Cannot prepare a restricted directory ACL.")
    try:
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        if not create(str(path), ctypes.byref(attributes)):
            raise catalog.Blocked("Cannot exclusively create the restricted backup directory.")
        verify_directory_owner(path, advapi, kernel)
    finally:
        free(descriptor)


def run_tool(tool, arguments, env=None, *, discard_stdout=False):
    result = subprocess.run(
        [str(tool), *arguments], env=env, stdout=subprocess.DEVNULL if discard_stdout else subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )
    if result.returncode or result.stderr:
        raise catalog.Blocked(f"{tool.stem} failed or emitted a warning. Tool details suppressed.")
    return result.stdout


def tools(directory):
    dump, restore = (directory / name for name in ("pg_dump.exe", "pg_restore.exe"))
    for tool in (dump, restore):
        if not tool.is_file():
            raise catalog.Blocked("The approved PostgreSQL 18.4 tools are missing.")
        if run_tool(tool, ["--version"]).split()[-1:] != [TOOL_VERSION.encode("ascii")]:
            raise catalog.Blocked("The backup and listing tools must both be PostgreSQL 18.4.")
    return dump, restore


def verify_scope(connection, identity, args):
    catalog.verify_identity(identity, args)
    if str(identity["version"]) != EXPECTED_VERSION:
        raise catalog.Blocked("PostgreSQL version differs from the reviewed Development version 17.11.")
    if not connection.execute(
        "SELECT pg_catalog.has_schema_privilege('public', 'USAGE') AS allowed"
    ).fetchone()["allowed"]:
        raise catalog.Blocked("The backup role cannot access the public schema.")
    scope = connection.execute(INVENTORY).fetchall()
    tables = {row["name"] for row in scope if row["kind"] == "r"}
    sequences = {row["name"] for row in scope if row["kind"] == "S"}
    if tables != TABLES or not BASELINE_SEQUENCES <= sequences or any(
        row["kind"] not in ("r", "S") or not row["readable"] or row["rls_active"]
        or row["inheritance"] or row["extension_member"]
        or not re.fullmatch(r"[a-z_][a-z0-9_]*", row["name"]) for row in scope
    ):
        raise catalog.Blocked("Cannot prove complete read access to the approved baseline tables and sequences.")
    if connection.execute("SELECT EXISTS (SELECT FROM pg_catalog.pg_largeobject_metadata)").fetchone()["exists"]:
        raise catalog.Blocked("Large objects are present; the approved archive scope cannot establish their coverage.")
    revisions = connection.execute("SELECT version_num FROM ONLY public.alembic_version LIMIT 2").fetchall()
    if [row["version_num"] for row in revisions] != [EXPECTED_REVISION]:
        raise catalog.Blocked("Installed Alembic revision differs from the approved Development baseline.")
    return sequences


def verify_listing(listing, sequences):
    entries = set(re.findall(rb"(?m)^\d+; \d+ \d+ (TABLE DATA|SEQUENCE SET) public ([a-z_0-9]+) ", listing))
    expected = {(b"TABLE DATA", name.encode("ascii")) for name in TABLES}
    expected.update((b"SEQUENCE SET", name.encode("ascii")) for name in sequences)
    if entries != expected:
        raise catalog.Blocked("Archive listing does not contain the complete approved table and sequence inventory.")


def verify_archive(restore, archive, sequences):
    verify_listing(run_tool(restore, ["--list", str(archive)]), sequences)
    # Unfiltered script output forces pg_restore to decode the payload. Direct it
    # to the OS null device from process creation so no SQL is kept or displayed.
    run_tool(restore, ["--format=custom", "--exit-on-error", "--file=-", str(archive)],
             discard_stdout=True)


def archive_checksum(archive):
    with archive.open("rb") as stream:
        if stream.read(5) != b"PGDMP":
            raise catalog.Blocked("Expected a PostgreSQL custom-format archive.")
        stream.seek(0)
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_manifest(destination, archive, checksum):
    if not checksum or not re.fullmatch(r"[a-f0-9]{64}", checksum):
        raise catalog.Blocked("A valid SHA-256 checksum is required to complete the backup.")
    manifest = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "project_reference": catalog.DEVELOPMENT,
        "postgresql_version": "17.11",
        "backup_tool_version": TOOL_VERSION,
        "alembic_revision": EXPECTED_REVISION,
        "archive": "development.dump",
        "archive_size_bytes": archive.stat().st_size,
        "sha256": checksum,
        "backup_scope": SCOPE,
        "status": "ARCHIVE CREATED AND VERIFIED",
        "restore_verified": False,
    }
    with (destination / "manifest.partial").open("x", encoding="utf-8") as output:
        json.dump(manifest, output, indent=2)
        output.write("\n")
    archive.rename(destination / "development.dump")
    (destination / "manifest.partial").rename(destination / "manifest.json")


def create_archive(destination, tool_pair, parameters, args, connect, row_factory, make_conninfo):
    dump, restore = tool_pair
    archive = destination / "development.dump.partial"
    with connect(**parameters, autocommit=True, row_factory=row_factory) as connection:
        if not connection.pgconn.ssl_in_use:
            raise catalog.Blocked("TLS is not active. No export was attempted.")
        connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        try:
            identity = connection.execute(catalog.IDENTITY).fetchone()
            sequences = verify_scope(connection, identity, args)
            snapshot = connection.execute("SELECT pg_catalog.pg_export_snapshot() AS snapshot").fetchone()["snapshot"]
            if not re.fullmatch(r"[A-Fa-f0-9]+-[A-Fa-f0-9]+-\d+", snapshot):
                raise catalog.Blocked("Could not verify an export snapshot.")
            create_restricted_directory(destination)
            child_env = dict(os.environ, PGPASSWORD=parameters["password"])
            try:
                conninfo = make_conninfo(**{name: value for name, value in parameters.items() if name != "password"})
                run_tool(dump, [
                    "--no-password", "--format=custom", "--schema=public", "--strict-names",
                    "--no-publications", "--no-subscriptions", "--lock-wait-timeout=10s",
                    f"--snapshot={snapshot}", f"--file={archive}", f"--dbname={conninfo}",
                ], child_env)
            finally:
                child_env.pop("PGPASSWORD", None)
        finally:
            connection.execute("ROLLBACK")
    verify_archive(restore, archive, sequences)
    write_manifest(destination, archive, archive_checksum(archive))


def require_confirmation(response):
    if response != "BACK UP DEVELOPMENT ONLY":
        raise catalog.Blocked("Backup confirmation was not supplied. No connection or export was attempted.")


def main():
    parser = catalog.SafeArguments(description=__doc__)
    parser.add_argument("--expected-host", required=True)
    parser.add_argument("--expected-port", required=True, type=int)
    parser.add_argument("--expected-backup-role", required=True, dest="expected_runtime_role")
    parser.add_argument("--backup-name", required=True)
    args = parser.parse_args()
    if (os.name != "nt" or not sys.stdin.isatty() or not sys.stderr.isatty()
            or not development.approved_host(args.expected_host.lower()) or args.expected_port != 5432
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,62}", args.expected_runtime_role)):
        raise catalog.Blocked("Use an interactive Windows terminal and an approved Development direct or session endpoint on port 5432.")
    if any(name.upper().startswith("PG") and name.upper() != "PGSSLROOTCERT" for name in os.environ):
        raise catalog.Blocked("Ambient libpq overrides are present. Use an approved clean terminal.")
    destination = destination_for(Path.home(), catalog.ROOT, args.backup_name)
    tool_pair = tools(BIN)
    print("Development project:", catalog.DEVELOPMENT)
    print("Local backup directory:", destination)
    require_confirmation(input("Type BACK UP DEVELOPMENT ONLY to connect and export: "))
    from psycopg import connect
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from psycopg.rows import dict_row

    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        parameters = development.connection_parameters(
            getpass.getpass("Development DATABASE_URL containing the password (hidden): "), args, conninfo_to_dict,
        )
    create_archive(destination, tool_pair, parameters, args, connect, dict_row, make_conninfo)
    print("ARCHIVE CREATED AND VERIFIED. Restore verification requires a separate approved restore.")


def cli():
    try:
        main()
        return 0
    except catalog.Blocked as exc:
        print(str(exc))
    except (Exception, KeyboardInterrupt):
        print("Backup stopped. Underlying error details were suppressed.")
    print("Only a backup with a completed manifest is valid. Retain any partial output for review.")
    return 1


if __name__ == "__main__":
    sys.exit(cli())
