$ErrorActionPreference = "Stop"

$ProjectDir = Resolve-Path (Join-Path $PSScriptRoot "..")
$AppName = "Shocks Art"
$Port = 8000
$Route = "/shocks_art"
$AppUrl = "https://desktop.tail27cee7.ts.net$Route/"
$HealthUrl = "http://127.0.0.1:$Port$Route/health"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message"
}

function Get-PortListenerProcessIds {
    try {
        return @(
            Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
                Select-Object -ExpandProperty OwningProcess -Unique
        )
    } catch {
        return @()
    }
}

function Get-PortListenerProcesses {
    foreach ($ProcessId in Get-PortListenerProcessIds) {
        Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId"
    }
}

function Test-IsShocksArtServerProcess {
    param($Process)
    return $Process.CommandLine -match "uvicorn\s+app\.main:app" -and
        $Process.CommandLine -match "--port\s+$Port"
}

function Test-AppHealth {
    try {
        $Response = Invoke-WebRequest -UseBasicParsing $HealthUrl -TimeoutSec 2
        return $Response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Wait-AppHealth {
    for ($Attempt = 1; $Attempt -le 20; $Attempt++) {
        Start-Sleep -Seconds 1
        if (Test-AppHealth) {
            return $true
        }
        Write-Host "Waiting... ($Attempt/20)"
    }
    return $false
}

function Configure-TailscaleRoutes {
    Write-Step "Configuring Tailscale routes"
    $Target = "http://127.0.0.1:$Port"

    try {
        & tailscale.exe funnel --https=443 --set-path=$Route --bg --yes $Target
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Could not configure the public Funnel route. The healthy local app is still running."
        }
    } catch {
        Write-Warning "Could not configure the public Funnel route: $($_.Exception.Message). The healthy local app is still running."
    }

    try {
        & tailscale.exe serve --https=8443 --set-path=$Route --bg --yes $Target
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Could not configure the private tailnet route. The healthy local app is still running."
        }
    } catch {
        Write-Warning "Could not configure the private tailnet route: $($_.Exception.Message). The healthy local app is still running."
    }
}

Set-Location -LiteralPath $ProjectDir

$Listeners = @(Get-PortListenerProcesses)
$AlreadyHealthy = $false
if ($Listeners.Count -gt 0) {
    foreach ($Listener in $Listeners) {
        if (-not (Test-IsShocksArtServerProcess $Listener)) {
            throw "Port $Port is in use by non-$AppName PID $($Listener.ProcessId): $($Listener.CommandLine)"
        }
    }

    if (Test-AppHealth) {
        $AlreadyHealthy = $true
        Write-Host "$AppName is already running and healthy on port $Port."
    } else {
        Write-Step "Restarting unhealthy $AppName listener"
        foreach ($Listener in $Listeners) {
            Write-Host "Stopping PID $($Listener.ProcessId)"
            Stop-Process -Id $Listener.ProcessId -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Seconds 2

        $Remaining = @(Get-PortListenerProcesses)
        if ($Remaining.Count -gt 0) {
            $Descriptions = @($Remaining | ForEach-Object { "PID $($_.ProcessId): $($_.CommandLine)" })
            throw "Port $Port is still occupied after stopping the unhealthy $AppName listener: $($Descriptions -join '; ')"
        }
    }
}

if (-not $AlreadyHealthy) {
    Write-Step "Starting $AppName"
    $LogDir = Join-Path $ProjectDir "data"
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    $StdOutLog = Join-Path $LogDir "server.out.log"
    $StdErrLog = Join-Path $LogDir "server.err.log"
    $Server = Start-Process -FilePath "py" `
        -ArgumentList @("-3.13", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", $Port) `
        -WorkingDirectory $ProjectDir `
        -PassThru `
        -WindowStyle Hidden `
        -RedirectStandardOutput $StdOutLog `
        -RedirectStandardError $StdErrLog
    Write-Host "Started PID $($Server.Id)"

    Write-Step "Waiting for health check"
    if (-not (Wait-AppHealth)) {
        throw "$AppName did not pass its health check. Expected: $HealthUrl. Logs: $StdOutLog, $StdErrLog"
    }
}

Configure-TailscaleRoutes

try {
    Start-Process $AppUrl
} catch {
    Write-Warning "Could not open $AppUrl automatically: $($_.Exception.Message)"
}

Write-Host "Ready: $AppUrl"
