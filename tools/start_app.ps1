. (Join-Path $PSScriptRoot "app_contract.ps1")

function Get-PythonLaunch {
    $VenvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $VenvPython) {
        return @{ FilePath = $VenvPython; PrefixArgs = @() }
    }

    if (Get-Command py -ErrorAction SilentlyContinue) {
        try {
            & py -3.13 -c "import sys" *> $null
            if ($LASTEXITCODE -eq 0) {
                return @{ FilePath = "py"; PrefixArgs = @("-3.13") }
            }
        } catch {}
        return @{ FilePath = "py"; PrefixArgs = @("-3") }
    }

    if (Get-Command python -ErrorAction SilentlyContinue) {
        return @{ FilePath = "python"; PrefixArgs = @() }
    }

    throw "No usable Python launcher found. Create .venv or install Python 3.11+."
}

Set-Location -LiteralPath $ProjectDir
New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null
$StdOutLog = Join-Path $LogsDir "app.out.log"
$StdErrLog = Join-Path $LogsDir "app.err.log"

$Existing = @(Get-PortListenerProcesses)
if ($Existing.Count -gt 0) {
    foreach ($Listener in $Existing) {
        if (-not (Test-OwnedServerProcess $Listener)) {
            throw "Port $Port is in use by non-$AppName PID $($Listener.ProcessId): $($Listener.CommandLine)"
        }
    }
    if (-not (Test-AppIdentity -TimeoutSeconds 4)) {
        throw "$AppName owns port $Port but is not healthy at $HealthUrl. Use Restart App.cmd or inspect $StdErrLog."
    }
    $OwnedProcessId = [int]$Existing[0].ProcessId
    Write-RuntimeMetadata -ProcessId $OwnedProcessId
    Write-Host "$AppName is already healthy on port $Port (PID $OwnedProcessId)."
} else {
    Write-Step "Starting $AppName"
    $Commit = Get-GitRevision
    $env:APP_PORT = "$Port"
    $env:APP_ROUTE = $Route
    $env:APP_BASE = $Route
    $env:JARVIS_APP_ID = $AppId
    $env:JARVIS_COMMIT_SHA = $Commit
    $env:JARVIS_MODE = "production"

    $Python = Get-PythonLaunch
    $QuotedProjectDir = '"' + $ProjectDir + '"'
    $Arguments = @($Python.PrefixArgs) + @(
        "-m", "uvicorn", "app.main:app",
        "--app-dir", $QuotedProjectDir,
        "--host", "127.0.0.1",
        "--port", "$Port"
    )
    $Server = Start-Process -FilePath $Python.FilePath `
        -ArgumentList $Arguments `
        -WorkingDirectory $ProjectDir `
        -PassThru `
        -WindowStyle Hidden `
        -RedirectStandardOutput $StdOutLog `
        -RedirectStandardError $StdErrLog
    Write-Host "Started PID $($Server.Id) at revision $Commit"

    Write-Step "Waiting for namespaced health check"
    if (-not (Wait-AppHealth)) {
        throw "$AppName did not become healthy within $StartupTimeoutSeconds seconds. Expected: $HealthUrl. Logs: $StdOutLog, $StdErrLog"
    }

    $StartedListeners = @(Get-PortListenerProcesses)
    if ($StartedListeners.Count -ne 1 -or -not (Test-OwnedServerProcess $StartedListeners[0])) {
        throw "$AppName became healthy, but ownership of port $Port could not be established unambiguously."
    }
    $OwnedProcessId = [int]$StartedListeners[0].ProcessId
    Write-RuntimeMetadata -ProcessId $OwnedProcessId
    if ($OwnedProcessId -ne $Server.Id) {
        Write-Host "Listening PID $OwnedProcessId (launcher PID $($Server.Id))"
    }
}

Write-Step "Validating local app contract"
if (-not (Test-HttpEndpoint -Url "$LocalBaseUrl/" -TimeoutSeconds 4)) {
    throw "Local contract verification failed: $LocalBaseUrl/"
}
if (-not (Test-AppIdentity -TimeoutSeconds 4)) {
    throw "Local contract verification failed: $HealthUrl did not identify as $AppId."
}
if (-not (Test-HttpEndpoint -Url "$LocalBaseUrl/api/ping" -TimeoutSeconds 4)) {
    throw "Local contract verification failed: $LocalBaseUrl/api/ping"
}

Write-Step "Configuring app-owned Tailscale routes"
$Network = Set-AppTailscaleRoutes
if ($Network.PublicOk) {
    Start-Sleep -Seconds 1
    Test-PublicContract -WarnOnly | Out-Null
}

Write-Step "Starting autonomous host bridge"
try {
    & (Join-Path $PSScriptRoot "start_host_worker.ps1")
} catch {
    Write-Warning "Autonomous host bridge did not start: $($_.Exception.Message)"
}

$AppUrl = "$PublicBaseUrl$Route/"
Write-Host "Ready: $AppUrl"
Write-Host "Health: $HealthUrl"
Write-Host "Tailscale upstream: $($Network.Target)"
