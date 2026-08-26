. (Join-Path $PSScriptRoot "app_contract.ps1")

function Stop-BridgeWorker {
    param(
        [Parameter(Mandatory = $true)][string]$ScriptName,
        [Parameter(Mandatory = $true)][string]$ProcessLabel,
        [Parameter(Mandatory = $true)][string]$PidFile
    )
    $PidPath = Join-Path $DataDir $PidFile
    if (-not (Test-Path -LiteralPath $PidPath)) { Write-Host "$ProcessLabel is already stopped."; return }
    $WorkerPid = 0
    [void][int]::TryParse((Get-Content -LiteralPath $PidPath -Raw).Trim(), [ref]$WorkerPid)
    if ($WorkerPid -le 0) { Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue; return }
    $Process = Get-CimInstance Win32_Process -Filter "ProcessId=$WorkerPid" -ErrorAction SilentlyContinue
    if (-not $Process) { Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue; Write-Host "$ProcessLabel was not running."; return }
    if ($Process.CommandLine -notmatch [regex]::Escape($ScriptName)) { throw "Refusing to stop PID $WorkerPid because it is not the Shock's Art $ProcessLabel." }
    Write-Host "Stopping $ProcessLabel PID $WorkerPid"
    Stop-Process -Id $WorkerPid -Force -ErrorAction Stop
    $Stopwatch = [Diagnostics.Stopwatch]::StartNew()
    while ($Stopwatch.Elapsed.TotalSeconds -lt 10) {
        if (-not (Get-Process -Id $WorkerPid -ErrorAction SilentlyContinue)) { Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue; Write-Host "$ProcessLabel stopped."; return }
        Start-Sleep -Milliseconds 250
    }
    throw "$ProcessLabel PID $WorkerPid did not stop."
}

Stop-BridgeWorker -ScriptName "google_update_worker.mjs" -ProcessLabel "Google update worker" -PidFile "google_update_worker.pid"
Stop-BridgeWorker -ScriptName "google_host_worker.py" -ProcessLabel "Google host verifier" -PidFile "google_host_worker.pid"
