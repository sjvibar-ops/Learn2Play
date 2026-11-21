param(
    [Parameter(Mandatory)][string]$ExpectedHost,
    [Parameter(Mandatory)][ValidateRange(1,65535)][int]$ExpectedPort,
    [Parameter(Mandatory)][string]$ExpectedRuntimeRole,
    [string]$ReceiptBucket = 'payment-receipts'
)

$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '../.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    Write-Error 'Use the installed Learn2Play virtual environment.'
}
& $python -I -B (Join-Path $PSScriptRoot 'supabase_development_preflight.py') --expected-host $ExpectedHost --expected-port $ExpectedPort --expected-runtime-role $ExpectedRuntimeRole --receipt-bucket $ReceiptBucket
exit $LASTEXITCODE
