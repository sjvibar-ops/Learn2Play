# Development database backup

Run this only with separate approval to export the confirmed Learn2Play Development database. Use interactive PowerShell, the installed Learn2Play virtual environment and PostgreSQL 18.4 tools. Confirm the approved direct database hostname, or exact Seoul **session** pooler hostname, port 5432, and SQL login role. Use the underlying role without the project suffix in the command. The prompted URL must contain the correct project-scoped login if pooled. Keep the URL out of chat, command arguments, and screenshots.

```powershell
.\scripts\supabase_development_backup.ps1 `
    -ExpectedHost (Read-Host 'Approved Development database host') `
    -ExpectedPort 5432 `
    -ExpectedBackupRole (Read-Host 'Approved SQL backup role') `
    -BackupName (Read-Host 'New local backup name')
```

Use a terminal with approved CA trust. If needed, point `PGSSLROOTCERT` to independently verified trust material. The tool requires hostname verification and rejects other ambient libpq overrides. Use a URL without an explicit SSL mode or one specifying `verify-full`. The password is supplied to the dump child in its environment for that process only. Run under your normal Windows user on a trusted workstation. Same-user or administrative process inspection is outside this protection.

The script verifies PostgreSQL 17.11, revision `9e45f7e7aa32`, and unrestricted read access for the approved baseline tables and sequences. A restrictive role, schema drift, an unexpectedly upgraded server, or an endpoint that cannot share an exported snapshot stops the run. It retains ownership and ACL metadata and writes a custom archive in a new restricted directory under your local user profile, outside Git. Database and Storage writers should be coordinated before a recovery capture to keep file references and sequence state consistent. The backup does not pause those writers.

Treat only a matching archive and completed `manifest.json` as an archive candidate. A SHA-256 checksum and a successful archive listing confirm integrity evidence and inventory, not a restore. Retain partial output privately for review, and use a fresh name for an approved retry. Keep the archive outside Git and protect it at rest. The Windows access controls are not encryption. Separately inventory the owners, cluster roles and memberships, and dependencies outside the public schema. Do not export password verifiers or re-create managed Supabase schemas in a plain PostgreSQL rehearsal. Review restored SQL and use an isolated PostgreSQL 18 target with no hosted access. PostgreSQL 18 tools can dump a PostgreSQL 17 source. Restoring the result onto a PostgreSQL 17 server is not guaranteed.

## Separate Storage backup

The database archive excludes Supabase Storage metadata and object bytes. Under separate approval, inventory bucket names, visibility, allowed content types, limits and policy metadata. Save a restricted manifest of object keys, sizes and checksums where available. Copy the bytes through an approved read-only Development Storage method, including private provider evidence and receipts if present. Validate the copied byte counts and checksums, and reconcile application file references against the manifest. Keep credentials, signed URLs and private contents out of reports. Use separate secure retention and restore procedures for the Storage copy.
