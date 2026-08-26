. (Join-Path $PSScriptRoot "app_contract.ps1")

$DefaultSpreadsheetId = "1hD8IqH_o1RJnVyxpAuhSmR-nB6DNwvK51xZIyn8IX_I"

function Import-ShocksHostEnvironment {
    $EnvPath = Join-Path $ProjectDir ".env"
    if (-not (Test-Path -LiteralPath $EnvPath)) { return }
    foreach ($Line in Get-Content -LiteralPath $EnvPath) {
        $Trimmed = $Line.Trim()
        if (-not $Trimmed -or $Trimmed.StartsWith("#") -or -not $Trimmed.Contains("=")) { continue }
        $Parts = $Trimmed.Split("=", 2); $Name = $Parts[0].Trim()
        if (-not $Name.StartsWith("SHOCKS_")) { continue }
        [Environment]::SetEnvironmentVariable($Name, $Parts[1].Trim().Trim('"').Trim("'"), "Process")
    }
}

function Test-HostWorkerPython {
    param([string]$FilePath, [string[]]$PrefixArgs)
    try {
        $ProbeArgs = @($PrefixArgs) + @("-c", "import google.oauth2, googleapiclient.discovery")
        & $FilePath @ProbeArgs *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Get-PythonLaunch {
    $Candidates = @()
    $VenvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $VenvPython) {
        $Candidates += @{ FilePath = $VenvPython; PrefixArgs = @() }
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $Candidates += @{ FilePath = "py"; PrefixArgs = @("-3.13") }
        $Candidates += @{ FilePath = "py"; PrefixArgs = @("-3") }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $Candidates += @{ FilePath = "python"; PrefixArgs = @() }
    }

    foreach ($Candidate in $Candidates) {
        if (Test-HostWorkerPython -FilePath $Candidate.FilePath -PrefixArgs $Candidate.PrefixArgs) {
            return $Candidate
        }
    }
    throw "No Python runtime with google-auth and google-api-python-client is available for the Google host worker."
}

Import-ShocksHostEnvironment
if (-not $env:SHOCKS_GOOGLE_SPREADSHEET_ID) {
    $env:SHOCKS_GOOGLE_SPREADSHEET_ID = $DefaultSpreadsheetId
    Write-Host "Using repository default Shock's Art GPT Bridge spreadsheet."
}
$PidPath = Join-Path $DataDir "google_host_worker.pid"; $StatusPath = Join-Path $DataDir "google_host_worker_status.json"
$OutLog = Join-Path $LogsDir "google_host_worker.out.log"; $ErrLog = Join-Path $LogsDir "google_host_worker.err.log"
New-Item -ItemType Directory -Force -Path $DataDir, $LogsDir | Out-Null
if (Test-Path -LiteralPath $PidPath) {
    $ExistingPid = 0; [void][int]::TryParse((Get-Content -LiteralPath $PidPath -Raw).Trim(), [ref]$ExistingPid)
    if ($ExistingPid -gt 0) {
        $Existing = Get-CimInstance Win32_Process -Filter "ProcessId=$ExistingPid" -ErrorAction SilentlyContinue
        if ($Existing -and $Existing.CommandLine -match 'google_host_worker\.py') { Write-Host "Google host worker is already running (PID $ExistingPid)."; exit 0 }
    }
    Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
}
$Python = Get-PythonLaunch; $Args = @($Python.PrefixArgs) + @("tools/google_host_worker.py")
$Worker = Start-Process -FilePath $Python.FilePath -ArgumentList $Args -WorkingDirectory $ProjectDir -PassThru -WindowStyle Hidden -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog
Set-Content -LiteralPath $PidPath -Value $Worker.Id -Encoding ascii
Start-Sleep -Seconds 2
if (-not (Get-Process -Id $Worker.Id -ErrorAction SilentlyContinue)) { Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue; throw "Google host worker exited during startup. Inspect $ErrLog" }
Write-Host "Started Google host worker PID $($Worker.Id). Status: $StatusPath"
