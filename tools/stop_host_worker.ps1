. (Join-Path $PSScriptRoot "app_contract.ps1")

$PidPath = Join-Path $DataDir "google_host_worker.pid"
if (-not (Test-Path -LiteralPath $PidPath)) {
    Write-Host "Google host worker is already stopped."
    exit 0
}

$WorkerPid = 0
[void][int]::TryParse((Get-Content -LiteralPath $PidPath -Raw).Trim(), [ref]$WorkerPid)
if ($WorkerPid -le 0) {
    Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
    Write-Host "Removed invalid Google host worker PID metadata."
    exit 0
}

$Process = Get-Process -Id $WorkerPid -ErrorAction SilentlyContinue
if (-not $Process) {
    Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
    Write-Host "Google host worker was not running."
    exit 0
}

Write-Host "Stopping Google host worker PID $WorkerPid"
Stop-Process -Id $WorkerPid -Force -ErrorAction Stop
$Stopwatch = [Diagnostics.Stopwatch]::StartNew()
while ($Stopwatch.Elapsed.TotalSeconds -lt 10) {
    if (-not (Get-Process -Id $WorkerPid -ErrorAction SilentlyContinue)) {
        Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
        Write-Host "Google host worker stopped."
        exit 0
    }
    Start-Sleep -Milliseconds 250
}
throw "Google host worker PID $WorkerPid did not stop."
