param(
    [Parameter(Mandatory)][string]$ExpectedHost,
    [Parameter(Mandatory)][ValidateRange(1,65535)][int]$ExpectedPort,
    [Parameter(Mandatory)][string]$ExpectedBackupRole,
    [Parameter(Mandatory)][string]$BackupName
)

$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '../.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Use the installed Learn2Play virtual environment.'
}
& $python -I -B (Join-Path $PSScriptRoot 'supabase_development_backup.py') --expected-host $ExpectedHost --expected-port $ExpectedPort --expected-backup-role $ExpectedBackupRole --backup-name $BackupName
exit $LASTEXITCODE
