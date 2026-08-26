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

function Test-BridgePython {
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
    if (Test-Path -LiteralPath $VenvPython) { $Candidates += @{ FilePath = $VenvPython; PrefixArgs = @() } }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $Candidates += @{ FilePath = "py"; PrefixArgs = @("-3.13") }
        $Candidates += @{ FilePath = "py"; PrefixArgs = @("-3") }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) { $Candidates += @{ FilePath = "python"; PrefixArgs = @() } }
    foreach ($Candidate in $Candidates) {
        if (Test-BridgePython -FilePath $Candidate.FilePath -PrefixArgs $Candidate.PrefixArgs) { return $Candidate }
    }
    throw "No Python runtime with google-auth and google-api-python-client is available for the Shock's Art GPT Bridge workers."
}

function Start-BridgeWorker {
    param(
        [Parameter(Mandatory = $true)]$Python,
        [Parameter(Mandatory = $true)][string]$ScriptName,
        [Parameter(Mandatory = $true)][string]$ProcessLabel,
        [Parameter(Mandatory = $true)][string]$PidFile,
        [Parameter(Mandatory = $true)][string]$OutLog,
        [Parameter(Mandatory = $true)][string]$ErrLog
    )
    $PidPath = Join-Path $DataDir $PidFile
    if (Test-Path -LiteralPath $PidPath) {
        $ExistingPid = 0
        [void][int]::TryParse((Get-Content -LiteralPath $PidPath -Raw).Trim(), [ref]$ExistingPid)
        if ($ExistingPid -gt 0) {
            $Existing = Get-CimInstance Win32_Process -Filter "ProcessId=$ExistingPid" -ErrorAction SilentlyContinue
            if ($Existing -and $Existing.CommandLine -match [regex]::Escape($ScriptName)) {
                Write-Host "$ProcessLabel is already running (PID $ExistingPid)."
                return
            }
        }
        Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
    }

    $Args = @($Python.PrefixArgs) + @("tools/$ScriptName")
    $Worker = Start-Process -FilePath $Python.FilePath -ArgumentList $Args -WorkingDirectory $ProjectDir -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogsDir $OutLog) -RedirectStandardError (Join-Path $LogsDir $ErrLog)
    Set-Content -LiteralPath $PidPath -Value $Worker.Id -Encoding ascii
    Start-Sleep -Seconds 2
    if (-not (Get-Process -Id $Worker.Id -ErrorAction SilentlyContinue)) {
        Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
        throw "$ProcessLabel exited during startup. Inspect $(Join-Path $LogsDir $ErrLog)"
    }
    Write-Host "Started $ProcessLabel PID $($Worker.Id)."
}

Import-ShocksHostEnvironment
if (-not $env:SHOCKS_GOOGLE_SPREADSHEET_ID) {
    $env:SHOCKS_GOOGLE_SPREADSHEET_ID = $DefaultSpreadsheetId
    Write-Host "Using repository default Shock's Art GPT Bridge spreadsheet."
}
# These workers are launched as scripts under tools/. Make the repository root
# importable so app.* resolves deterministically without depending on caller cwd.
if ($env:PYTHONPATH) { $env:PYTHONPATH = "$ProjectDir;$($env:PYTHONPATH)" }
else { $env:PYTHONPATH = $ProjectDir }

New-Item -ItemType Directory -Force -Path $DataDir, $LogsDir | Out-Null
$Python = Get-PythonLaunch

Start-BridgeWorker -Python $Python -ScriptName "google_host_worker.py" -ProcessLabel "Google host verifier" `
    -PidFile "google_host_worker.pid" -OutLog "google_host_worker.out.log" -ErrLog "google_host_worker.err.log"
Start-BridgeWorker -Python $Python -ScriptName "google_update_worker.py" -ProcessLabel "Google update worker" `
    -PidFile "google_update_worker.pid" -OutLog "google_update_worker.out.log" -ErrLog "google_update_worker.err.log"
