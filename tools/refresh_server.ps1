$ErrorActionPreference = "Stop"

$ProjectDir = Resolve-Path (Join-Path $PSScriptRoot "..")
$AppUrl = "https://desktop.tail27cee7.ts.net/shocks_art/"
$Port = 8000
$HealthUrls = @(
    "http://127.0.0.1:$Port/shocks_art/",
    "http://127.0.0.1:$Port/shocks_art/library"
)

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

Set-Location -LiteralPath $ProjectDir

Write-Step "Pulling latest code"
git pull --ff-only
if ($LASTEXITCODE -ne 0) {
    throw "git pull failed. Resolve the Git message above, then run this script again."
}
$Branch = (git branch --show-current).Trim()
$Commit = (git rev-parse --short HEAD).Trim()
Write-Host "Running $Branch at $Commit"

Write-Step "Stopping existing Shocks Art server"
$ProcessIds = @()
$ProcessIds += @(Get-ShocksArtServerProcess | Select-Object -ExpandProperty ProcessId)
$ProcessIds += @(Get-PortListenerProcessIds)
$ProcessIds = @($ProcessIds | Where-Object { $_ } | Sort-Object -Unique)

if ($ProcessIds.Count -eq 0) {
    Write-Host "No existing Shocks Art server process found."
} else {
    foreach ($ProcessId in $ProcessIds) {
        Write-Host "Stopping PID $ProcessId"
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
}

$RemainingListeners = @(Get-PortListenerProcessIds)
if ($RemainingListeners.Count -gt 0) {
    throw "Port $Port is still in use by PID(s): $($RemainingListeners -join ', '). Stop them and run refresh again."
}

Write-Step "Starting Shocks Art server"
$ServerCommand = 'cd /d "' + $ProjectDir + '" && py -3.13 -m uvicorn app.main:app --host 127.0.0.1 --port ' + $Port
Start-Process -FilePath "cmd.exe" -ArgumentList "/k", $ServerCommand -WorkingDirectory $ProjectDir

Write-Step "Waiting for server"
$Ready = $false
for ($Attempt = 1; $Attempt -le 20; $Attempt++) {
    Start-Sleep -Seconds 1
    $AllHealthy = $true
    foreach ($HealthUrl in $HealthUrls) {
        try {
            $Response = Invoke-WebRequest -UseBasicParsing $HealthUrl -TimeoutSec 2
            if ($Response.StatusCode -ne 200) {
                $AllHealthy = $false
                break
            }
        } catch {
            $AllHealthy = $false
            break
        }
    }
    if ($AllHealthy) {
        $Ready = $true
        break
    }
    Write-Host "Waiting... ($Attempt/20)"
}

if (-not $Ready) {
    throw "Server did not pass health checks within 20 seconds. Check the new server window. Expected: $($HealthUrls -join ', ')"
}

Write-Step "Opening app"
Start-Process $AppUrl
Write-Host "Ready: $AppUrl ($Branch $Commit)"
