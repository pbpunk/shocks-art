$ErrorActionPreference = "Stop"

$ProjectDir = Resolve-Path (Join-Path $PSScriptRoot "..")
$AppUrl = "https://desktop.tail27cee7.ts.net/shocks_art/"
$HealthUrl = "http://127.0.0.1:8000/shocks_art/"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message"
}

function Get-ShocksArtServerProcess {
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.CommandLine -match "uvicorn\s+app\.main:app" -and
            $_.CommandLine -match "--port\s+8000"
        }
}

Set-Location -LiteralPath $ProjectDir

Write-Step "Pulling latest code"
git pull --ff-only
if ($LASTEXITCODE -ne 0) {
    throw "git pull failed. Resolve the Git message above, then run this script again."
}

Write-Step "Stopping existing Shocks Art server"
$Processes = @(Get-ShocksArtServerProcess)
if ($Processes.Count -eq 0) {
    Write-Host "No existing Shocks Art server process found."
} else {
    foreach ($ProcessId in @($Processes.ProcessId | Sort-Object -Unique)) {
        Write-Host "Stopping PID $ProcessId"
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
}

Write-Step "Starting Shocks Art server"
$ServerCommand = 'cd /d "' + $ProjectDir + '" && py -3.13 -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000'
Start-Process -FilePath "cmd.exe" -ArgumentList "/k", $ServerCommand -WorkingDirectory $ProjectDir

Write-Step "Waiting for server"
$Ready = $false
for ($Attempt = 1; $Attempt -le 20; $Attempt++) {
    Start-Sleep -Seconds 1
    try {
        $Response = Invoke-WebRequest -UseBasicParsing $HealthUrl -TimeoutSec 2
        if ($Response.StatusCode -eq 200) {
            $Ready = $true
            break
        }
    } catch {
        Write-Host "Waiting... ($Attempt/20)"
    }
}

if (-not $Ready) {
    throw "Server did not respond at $HealthUrl within 20 seconds. Check the new server window."
}

Write-Step "Opening app"
Start-Process $AppUrl
Write-Host "Ready: $AppUrl"
