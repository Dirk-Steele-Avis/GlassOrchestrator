param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("A", "B")]
    [string]$Label,

    [string]$OutputRoot = "log",
    [string]$TestsPath = "tests"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$baselineDir = Join-Path $OutputRoot ("baseline-" + $timestamp + "-" + $Label)
New-Item -ItemType Directory -Force -Path $baselineDir | Out-Null

$metadataPath = Join-Path $baselineDir "metadata.json"
$xmlPath = Join-Path $baselineDir "pytest-results.xml"
$consolePath = Join-Path $baselineDir "pytest-console.txt"
$summaryPath = Join-Path $baselineDir "summary.json"

$venvPython = Join-Path ".venv" "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw "Virtual environment python not found at .venv\\Scripts\\python.exe"
}

$metadata = [ordered]@{
    timestamp = (Get-Date).ToString("o")
    label = $Label
    branch = (git rev-parse --abbrev-ref HEAD).Trim()
    commit = (git rev-parse HEAD).Trim()
    python = ((& $venvPython -c "import sys;print(sys.version.replace('\\n',' '))") | Out-String).Trim()
    pytest = ((& $venvPython -c "import pytest;print(pytest.__version__)") | Out-String).Trim()
    platform = ((& $venvPython -c "import platform;print(platform.platform())") | Out-String).Trim()
    flags = [ordered]@{
        GLASS_RUN_E2E_TESTS = $env:GLASS_RUN_E2E_TESTS
        GLASS_RUN_LIVE_SHEETS_TESTS = $env:GLASS_RUN_LIVE_SHEETS_TESTS
    }
    credentials_present = [ordered]@{
        GLASS_EMAIL_ACCOUNT = [bool]$env:GLASS_EMAIL_ACCOUNT
        GLASS_EMAIL_PASSWORD = [bool]$env:GLASS_EMAIL_PASSWORD
        GLASS_LOGIN_USERNAME = [bool]$env:GLASS_LOGIN_USERNAME
        GLASS_LOGIN_PASSWORD = [bool]$env:GLASS_LOGIN_PASSWORD
        GLASS_LOGIN_ID = [bool]$env:GLASS_LOGIN_ID
        SERVICE_ACCOUNT_JSON = (Test-Path "Service_account.json")
    }
}
$metadata | ConvertTo-Json -Depth 6 | Set-Content -Path $metadataPath

$stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
& $venvPython -m pytest $TestsPath -v --tb=short --junit-xml "$xmlPath" 2>&1 | Tee-Object -FilePath $consolePath
$pytestExit = $LASTEXITCODE
$stopwatch.Stop()

$summary = [ordered]@{
    baseline_dir = $baselineDir
    label = $Label
    pytest_exit_code = $pytestExit
    elapsed_seconds = [Math]::Round($stopwatch.Elapsed.TotalSeconds, 2)
    metadata = $metadataPath
    junit_xml = $xmlPath
    console_log = $consolePath
}
$summary | ConvertTo-Json -Depth 6 | Set-Content -Path $summaryPath

Write-Output ("BASELINE_DIR=" + $baselineDir)
Write-Output ("PYTEST_EXIT_CODE=" + $pytestExit)
Write-Output ("SUMMARY_FILE=" + $summaryPath)

exit $pytestExit
