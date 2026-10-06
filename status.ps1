# Current report for a Moon Sisters collection run.
#   .\status.ps1            -> run-1m
#   .\status.ps1 my-run     -> output\browser-runs\my-run
param([string]$Run = "run-1m")

$root = $PSScriptRoot
$runDir = Join-Path $root "output\browser-runs\$Run"
if (-not (Test-Path $runDir)) {
    Write-Host "No run folder: $runDir" -ForegroundColor Red
    exit 1
}

$collector = Get-CimInstance Win32_Process |
    Where-Object { $_.Name -eq "python.exe" -and $_.CommandLine -like "*collect_moon_browser*--run-name $Run*" }
if ($collector) {
    Write-Host "collector     running, pid $($collector.ProcessId -join ', ')" -ForegroundColor Green
} elseif (Test-Path (Join-Path $runDir "run.json")) {
    Write-Host "collector     finished" -ForegroundColor Green
} else {
    Write-Host "collector     NOT RUNNING and run.json missing: the run was stopped or crashed" -ForegroundColor Red
}

python (Join-Path $root "tools\run_status.py") $runDir
