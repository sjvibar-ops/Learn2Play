$ErrorActionPreference = "Stop"

$pythonExe = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
$pythonExe = [System.IO.Path]::GetFullPath($pythonExe)

if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw "Project virtual-environment Python was not found."
}

# READ-ONLY diagnostic: the embedded Python issues SELECT queries only.
$diagnosticCode = @'
import os
import re
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

EXPECTED_PROJECT_REF = "pxiwoowpoewxvnsjwfdw"
APPLICATION_TABLES = [
    "users",
    "appointments",
    "credentials",
    "mentor_availability",
    "mentor_unavailable_dates",
    "role_applications",
    "messages",
    "reports",
]

def fail(message):
    raise SystemExit(message)

def report_query_failure(stage, exc):
    error_type = type(exc).__name__
    original = getattr(exc, "orig", None)
    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    safe_sqlstate = f"; SQLSTATE {sqlstate}" if isinstance(sqlstate, str) and re.fullmatch(r"[0-9A-Z]{5}", sqlstate) else ""
    safe_messages = {
        "connection": "Could not establish the PostgreSQL connection; details suppressed.",
        "role query": "Could not read PostgreSQL role metadata; details suppressed.",
        "table query": "Could not read application table metadata; details suppressed.",
    }
    print(
        f"Diagnostic failed at {stage}: exception type {error_type}; "
        f"{safe_messages[stage]}{safe_sqlstate}",
        file=sys.stderr,
    )
    raise SystemExit(1)

database_url = os.environ.get("DATABASE_URL")
if not database_url:
    fail("Diagnostic failed: DATABASE_URL is not set.")

try:
    url = make_url(database_url)
except Exception:
    fail("Diagnostic failed: DATABASE_URL is not a valid SQLAlchemy URL.")

# Match the PostgreSQL URL normalization used by config.py.
if url.drivername in ("postgres", "postgresql", "postgresql+psycopg2"):
    url = url.set(drivername="postgresql+psycopg")
elif url.drivername != "postgresql+psycopg":
    fail("Diagnostic failed: DATABASE_URL is not a supported PostgreSQL URL.")

# Accept only the project's pooler URL form or its standard direct URL form.
host = (url.host or "").lower()
username = url.username or ""
pooler_host = re.fullmatch(r"aws-\d+-[a-z0-9-]+\.pooler\.supabase\.com", host)
direct_host = host == f"db.{EXPECTED_PROJECT_REF}.supabase.co"
if pooler_host:
    if username != f"postgres.{EXPECTED_PROJECT_REF}":
        fail("Diagnostic failed: pooler URL is not for the expected Development project.")
elif direct_host:
    if username != "postgres":
        fail("Diagnostic failed: direct URL has an unexpected database role.")
else:
    fail("Diagnostic failed: URL does not match a supported Development Supabase format.")

engine = None
connection = None
try:
    try:
        engine = create_engine(url, pool_pre_ping=True)
        connection = engine.connect()
    except Exception as exc:
        report_query_failure("connection", exc)

    try:
        role_state = connection.execute(text("""
            SELECT
                current_database() AS database_name,
                current_user AS database_role,
                r.rolsuper AS is_superuser,
                r.rolbypassrls AS bypasses_rls
            FROM pg_roles AS r
            WHERE r.rolname = current_user
        """)).mappings().one()
    except Exception as exc:
        report_query_failure("role query", exc)

    try:
        table_states = connection.execute(text("""
            SELECT
                expected.table_name,
                c.relname IS NOT NULL AS table_exists,
                owner_role.rolname AS table_owner,
                CASE
                    WHEN c.relname IS NULL THEN NULL
                    ELSE c.relowner = active_role.oid
                END AS current_role_owns_table,
                c.relrowsecurity AS rls_enabled,
                c.relforcerowsecurity AS rls_forced
            FROM unnest(CAST(:table_names AS text[])) AS expected(table_name)
            LEFT JOIN pg_namespace AS n
                ON n.nspname = 'public'
            LEFT JOIN pg_class AS c
                ON c.relnamespace = n.oid
               AND c.relname = expected.table_name
               AND c.relkind IN ('r', 'p')
            LEFT JOIN pg_roles AS owner_role
                ON owner_role.oid = c.relowner
            CROSS JOIN (
                SELECT oid
                FROM pg_roles
                WHERE rolname = current_user
            ) AS active_role
            ORDER BY expected.table_name
        """), {"table_names": APPLICATION_TABLES}).mappings().all()
    except Exception as exc:
        report_query_failure("table query", exc)

    if role_state["database_name"] != "postgres":
        fail("Diagnostic failed: connected database is not postgres.")

    print("READ-ONLY RLS / PostgreSQL permission diagnostic")
    print(f"Database: {role_state['database_name']}")
    print(f"Current database role: {role_state['database_role']}")
    print(f"Role is SUPERUSER: {role_state['is_superuser']}")
    print(f"Role has BYPASSRLS: {role_state['bypasses_rls']}")
    print("Application table ownership and RLS state (public schema):")
    for table in table_states:
        if not table["table_exists"]:
            print(f"  {table['table_name']}: table missing")
            continue
        print(
            f"  {table['table_name']}: owner={table['table_owner']}, "
            f"current_role_owns_table={table['current_role_owns_table']}, "
            f"rls_enabled={table['rls_enabled']}, "
            f"rls_forced={table['rls_forced']}"
        )
finally:
    if connection is not None:
        connection.close()
    if engine is not None:
        engine.dispose()
'@

function Invoke-WithSecurePreviewDatabase {
    param([scriptblock]$Action)

    $hadDatabaseUrl = Test-Path Env:DATABASE_URL
    $previousDatabaseUrl = [Environment]::GetEnvironmentVariable("DATABASE_URL", "Process")
    $secureUrl = Read-Host "Enter the Vercel Preview DATABASE_URL for the Development project" -AsSecureString
    $urlPointer = [IntPtr]::Zero

    try {
        $urlPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureUrl)
        $env:DATABASE_URL = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($urlPointer)
        & $Action
    }
    finally {
        if ($hadDatabaseUrl) {
            $env:DATABASE_URL = $previousDatabaseUrl
        }
        else {
            Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
        }

        if ($urlPointer -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($urlPointer)
        }
        $secureUrl.Dispose()
    }
}

Invoke-WithSecurePreviewDatabase {
    $temporaryPythonFile = Join-Path ([System.IO.Path]::GetTempPath()) ([System.IO.Path]::ChangeExtension([System.IO.Path]::GetRandomFileName(), ".py"))

    try {
        $utf8WithoutBom = [System.Text.UTF8Encoding]::new($false)
        [System.IO.File]::WriteAllText($temporaryPythonFile, $diagnosticCode, $utf8WithoutBom)

        $previousErrorActionPreference = $ErrorActionPreference
        try {
            # Keep Python stderr visible and non-terminating; use its exit code.
            $ErrorActionPreference = "Continue"
            & $pythonExe $temporaryPythonFile
            $diagnosticExitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }

        if ($null -eq $diagnosticExitCode -or $diagnosticExitCode -ne 0) {
            throw "Read-only diagnostic failed."
        }
    }
    finally {
        if (Test-Path -LiteralPath $temporaryPythonFile) {
            Remove-Item -LiteralPath $temporaryPythonFile -Force
        }
    }
}
