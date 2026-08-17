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

function Get-ShocksArtServerProcess {
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.CommandLine -match "uvicorn\s+app\.main:app" -and
            $_.CommandLine -match "--port\s+$Port"
        }
}

function Get-PortListenerProcesses {
    foreach ($ProcessId in Get-PortListenerProcessIds) {
        Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId"
    }
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

function Get-ChildProcesses {
    param([array]$ParentProcessIds)
    Get-CimInstance Win32_Process |
        Where-Object { $ParentProcessIds -contains $_.ParentProcessId }
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

Write-Step "Pulling latest code"
git fetch origin main --prune
if ($LASTEXITCODE -ne 0) {
    throw "git fetch failed. Resolve the Git message above, then run this script again."
}
git pull --ff-only origin main
if ($LASTEXITCODE -ne 0) {
    throw "git pull failed. Resolve the Git message above, then run this script again."
}
$Branch = (git branch --show-current).Trim()
$Commit = (git rev-parse --short HEAD).Trim()
Write-Host "Running $Branch at $Commit"

Write-Step "Stopping existing $AppName server"
$ProcessIds = @()
$ProcessIds += @(Get-ShocksArtServerProcess | Select-Object -ExpandProperty ProcessId)
$PortListenerIds = @(Get-PortListenerProcessIds)
$PortListeners = @(Get-PortListenerProcesses)
foreach ($Listener in $PortListeners) {
    if (-not (Test-IsShocksArtServerProcess $Listener)) {
        throw "Port $Port is in use by non-$AppName PID $($Listener.ProcessId): $($Listener.CommandLine)"
    }
    $ProcessIds += $Listener.ProcessId
}
$PortListenerChildren = @(Get-ChildProcesses $PortListenerIds)
foreach ($Child in $PortListenerChildren) {
    if (
        $Child.ExecutablePath -like "*Python313\python.exe" -and
        $Child.CommandLine -match "multiprocessing\.spawn"
    ) {
        $ProcessIds += $Child.ProcessId
    }
}
$ProcessIds += $PortListenerIds
$ProcessIds = @($ProcessIds | Where-Object { $_ } | Sort-Object -Unique)

if ($ProcessIds.Count -eq 0) {
    Write-Host "No existing $AppName server process found."
} else {
    foreach ($ProcessId in $ProcessIds) {
        Write-Host "Stopping PID $ProcessId"
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
}

$RemainingListeners = @(Get-PortListenerProcesses)
if ($RemainingListeners.Count -gt 0) {
    $RemainingDescriptions = @($RemainingListeners | ForEach-Object { "PID $($_.ProcessId): $($_.CommandLine)" })
    throw "Port $Port is still in use by $($RemainingDescriptions -join '; '). Stop it and run update again."
}

Write-Step "Starting $AppName server"
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
$Ready = $false
for ($Attempt = 1; $Attempt -le 20; $Attempt++) {
    Start-Sleep -Seconds 1
    if (Test-AppHealth) {
        $Ready = $true
        break
    }
    Write-Host "Waiting... ($Attempt/20)"
}

if (-not $Ready) {
    throw "Server did not pass its health check within 20 seconds. Expected: $HealthUrl. Logs: $StdOutLog, $StdErrLog"
}

Configure-TailscaleRoutes

Write-Step "Opening app"
Start-Process $AppUrl
Write-Host "Ready: $AppUrl ($Branch $Commit)"
