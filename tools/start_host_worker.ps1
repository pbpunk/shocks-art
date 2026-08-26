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

function Test-PythonRuntime {
    param([string]$FilePath, [string[]]$PrefixArgs)
    try {
        $ProbeArgs = @($PrefixArgs) + @("-c", "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)")
        & $FilePath @ProbeArgs *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
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

function Get-PythonCandidates {
    $Candidates = @()
    $VenvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $VenvPython) { $Candidates += @{ FilePath = $VenvPython; PrefixArgs = @() } }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $Candidates += @{ FilePath = "py"; PrefixArgs = @("-3.13") }
        $Candidates += @{ FilePath = "py"; PrefixArgs = @("-3") }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) { $Candidates += @{ FilePath = "python"; PrefixArgs = @() } }
    return $Candidates
}

function Get-BridgePython {
    $Candidates = @(Get-PythonCandidates)
    foreach ($Candidate in $Candidates) {
        if ((Test-PythonRuntime -FilePath $Candidate.FilePath -PrefixArgs $Candidate.PrefixArgs) -and
            (Test-BridgePython -FilePath $Candidate.FilePath -PrefixArgs $Candidate.PrefixArgs)) {
            return $Candidate
        }
    }

    $BasePython = $null
    foreach ($Candidate in $Candidates) {
        if (Test-PythonRuntime -FilePath $Candidate.FilePath -PrefixArgs $Candidate.PrefixArgs) {
            $BasePython = $Candidate
            break
        }
    }
    if (-not $BasePython) {
        throw "No Python 3.11+ runtime is available to bootstrap the Shock's Art GPT Bridge."
    }

    $BridgeVenv = Join-Path $DataDir "google_bridge_venv"
    $BridgePython = Join-Path $BridgeVenv "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $BridgePython)) {
        Write-Host "Creating managed Google bridge Python environment..."
        $BaseExecutable = [string]$BasePython.FilePath
        $VenvArgs = @($BasePython.PrefixArgs) + @("-m", "venv", $BridgeVenv)
        & $BaseExecutable @VenvArgs
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $BridgePython)) {
            throw "Could not create managed Google bridge Python environment at $BridgeVenv."
        }
    }

    if (-not (Test-BridgePython -FilePath $BridgePython -PrefixArgs @())) {
        Write-Host "Installing Google bridge dependencies into the managed environment..."
        & $BridgePython -m pip install --disable-pip-version-check --quiet `
            "google-auth>=2.32,<3" "google-api-python-client>=2.137,<3"
        if ($LASTEXITCODE -ne 0) {
            throw "Could not install Google bridge dependencies into $BridgeVenv."
        }
    }

    if (-not (Test-BridgePython -FilePath $BridgePython -PrefixArgs @())) {
        throw "Managed Google bridge Python environment is missing required Google libraries after installation."
    }
    return @{ FilePath = $BridgePython; PrefixArgs = @() }
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
$Python = Get-BridgePython

try {
    Start-BridgeWorker -Python $Python -ScriptName "google_host_worker.py" -ProcessLabel "Google host verifier" `
        -PidFile "google_host_worker.pid" -OutLog "google_host_worker.out.log" -ErrLog "google_host_worker.err.log"
} catch {
    Write-Warning "Google host verifier did not start: $($_.Exception.Message)"
}

try {
    Start-BridgeWorker -Python $Python -ScriptName "google_update_worker.py" -ProcessLabel "Google update worker" `
        -PidFile "google_update_worker.pid" -OutLog "google_update_worker.out.log" -ErrLog "google_update_worker.err.log"
} catch {
    Write-Warning "Google update worker did not start: $($_.Exception.Message)"
}
