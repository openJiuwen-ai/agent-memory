param(
    [switch]$Run
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"

Set-Location $RepoRoot
if (-not (Test-Path -LiteralPath $Python)) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.11 -m venv .venv
    } else {
        & python -m venv .venv
    }
}

& $Python -m pip install -r evaluation\environment\requirements.txt
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (-not (Test-Path -LiteralPath "evaluation\environment\.env")) {
    Copy-Item -LiteralPath "evaluation\environment\.env.example" -Destination "evaluation\environment\.env"
    Write-Host "Created evaluation\environment\.env. Fill in the required keys, then run again."
    exit 2
}

if ($Run) {
    & (Join-Path $PSScriptRoot "run.ps1")
    exit $LASTEXITCODE
}

Write-Host "Setup complete. Run .\evaluation\run.ps1 to start the end-to-end evaluation."
