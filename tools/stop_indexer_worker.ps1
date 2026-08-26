. (Join-Path $PSScriptRoot "app_contract.ps1")

$PidPath = Join-Path $DataDir "indexer_worker.pid"
if (-not (Test-Path -LiteralPath $PidPath)) { Write-Host "Library indexer is already stopped."; exit 0 }

$WorkerPid = 0
[void][int]::TryParse((Get-Content -LiteralPath $PidPath -Raw).Trim(), [ref]$WorkerPid)
if ($WorkerPid -le 0) {
    Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
    Write-Host "Removed stale Library indexer PID file."
    exit 0
}

$Process = Get-CimInstance Win32_Process -Filter "ProcessId=$WorkerPid" -ErrorAction SilentlyContinue
if (-not $Process) {
    Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
    Write-Host "Library indexer is already stopped."
    exit 0
}
if ($Process.CommandLine -notmatch [regex]::Escape("app.indexing.worker")) {
    throw "Refusing to stop PID $WorkerPid because it is not an owned Library indexer: $($Process.CommandLine)"
}

Write-Host "Stopping Library indexer PID $WorkerPid"
Stop-Process -Id $WorkerPid -Force -ErrorAction Stop
$Stopwatch = [Diagnostics.Stopwatch]::StartNew()
while ($Stopwatch.Elapsed.TotalSeconds -lt 10) {
    if (-not (Get-Process -Id $WorkerPid -ErrorAction SilentlyContinue)) {
        Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
        Write-Host "Library indexer stopped."
        exit 0
    }
    Start-Sleep -Milliseconds 250
}
throw "Library indexer PID $WorkerPid did not stop within 10 seconds."