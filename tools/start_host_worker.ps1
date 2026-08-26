. (Join-Path $PSScriptRoot "app_contract.ps1")

$DefaultSpreadsheetId = "1hD8IqH_o1RJnVyxpAuhSmR-nB6DNwvK51xZIyn8IX_I"

function Import-ShocksHostEnvironment {
    $EnvPath = Join-Path $ProjectDir ".env"
    if (-not (Test-Path -LiteralPath $EnvPath)) { return }
    foreach ($Line in Get-Content -LiteralPath $EnvPath) {
        $Trimmed = $Line.Trim()
        if (-not $Trimmed -or $Trimmed.StartsWith("#") -or -not $Trimmed.Contains("=")) { continue }
        $Parts = $Trimmed.Split("=", 2); $Name = $Parts[0].Trim()
        if ($Name.StartsWith("SHOCKS_")) {
            [Environment]::SetEnvironmentVariable($Name, $Parts[1].Trim().Trim('"').Trim("'"), "Process")
        }
    }
}

function Start-BridgeProcess {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Signature,
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
            if ($Existing -and $Existing.CommandLine -match [regex]::Escape($Signature)) {
                Write-Host "$ProcessLabel is already running (PID $ExistingPid)."
                return
            }
        }
        Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
    }
    $Worker = Start-Process -FilePath $Executable -ArgumentList $Arguments -WorkingDirectory $ProjectDir -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogsDir $OutLog) -RedirectStandardError (Join-Path $LogsDir $ErrLog)
    Set-Content -LiteralPath $PidPath -Value $Worker.Id -Encoding ascii
    Start-Sleep -Seconds 2
    if (-not (Get-Process -Id $Worker.Id -ErrorAction SilentlyContinue)) {
        Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
        throw "$ProcessLabel exited during startup. Inspect $(Join-Path $LogsDir $ErrLog)"
    }
    Write-Host "Started $ProcessLabel PID $($Worker.Id)."
}

function Test-PythonRuntime {
    param([string]$FilePath, [string[]]$PrefixArgs)
    try { & $FilePath @PrefixArgs -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" *> $null; return $LASTEXITCODE -eq 0 } catch { return $false }
}
function Test-BridgePython {
    param([string]$FilePath, [string[]]$PrefixArgs)
    try { & $FilePath @PrefixArgs -c "import google.oauth2, googleapiclient.discovery" *> $null; return $LASTEXITCODE -eq 0 } catch { return $false }
}
function Get-BridgePython {
    $Candidates = @()
    $VenvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $VenvPython) { $Candidates += @{ FilePath = $VenvPython; PrefixArgs = @() } }
    if (Get-Command py -ErrorAction SilentlyContinue) { $Candidates += @{ FilePath = "py"; PrefixArgs = @("-3.13") }; $Candidates += @{ FilePath = "py"; PrefixArgs = @("-3") } }
    if (Get-Command python -ErrorAction SilentlyContinue) { $Candidates += @{ FilePath = "python"; PrefixArgs = @() } }
    foreach ($Candidate in $Candidates) {
        if ((Test-PythonRuntime $Candidate.FilePath $Candidate.PrefixArgs) -and (Test-BridgePython $Candidate.FilePath $Candidate.PrefixArgs)) { return $Candidate }
    }
    $BasePython = $Candidates | Where-Object { Test-PythonRuntime $_.FilePath $_.PrefixArgs } | Select-Object -First 1
    if (-not $BasePython) { throw "No Python 3.11+ runtime is available for host verification." }
    $BridgeVenv = Join-Path $DataDir "google_bridge_venv"; $BridgePython = Join-Path $BridgeVenv "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $BridgePython)) {
        $BaseExecutable = [string]$BasePython.FilePath; $VenvArgs = @($BasePython.PrefixArgs) + @("-m", "venv", $BridgeVenv)
        & $BaseExecutable @VenvArgs
        if ($LASTEXITCODE -ne 0) { throw "Could not create managed verifier environment." }
    }
    if (-not (Test-BridgePython $BridgePython @())) {
        & $BridgePython -m pip install --disable-pip-version-check --quiet "google-auth>=2.32,<3" "google-api-python-client>=2.137,<3"
        if ($LASTEXITCODE -ne 0) { throw "Could not install verifier Google dependencies." }
    }
    return @{ FilePath = $BridgePython; PrefixArgs = @() }
}

Import-ShocksHostEnvironment
if (-not $env:SHOCKS_GOOGLE_SPREADSHEET_ID) { $env:SHOCKS_GOOGLE_SPREADSHEET_ID = $DefaultSpreadsheetId }
if ($env:PYTHONPATH) { $env:PYTHONPATH = "$ProjectDir;$($env:PYTHONPATH)" } else { $env:PYTHONPATH = $ProjectDir }
New-Item -ItemType Directory -Force -Path $DataDir, $LogsDir | Out-Null

# The updater is the rescue path. Start it first with Node built-ins only so Python/verifier failures cannot block updates.
try {
    $Node = Get-Command node -ErrorAction Stop
    Start-BridgeProcess -Executable $Node.Source -Arguments @("tools/google_update_worker.mjs") -Signature "google_update_worker.mjs" `
        -ProcessLabel "Google update worker" -PidFile "google_update_worker.pid" -OutLog "google_update_worker.out.log" -ErrLog "google_update_worker.err.log"
} catch {
    Write-Warning "Google update worker did not start: $($_.Exception.Message)"
}

# Host verification is independent and may require Python packages/profile configuration.
try {
    $Python = Get-BridgePython
    $VerifierArgs = @($Python.PrefixArgs) + @("tools/google_host_worker.py")
    Start-BridgeProcess -Executable $Python.FilePath -Arguments $VerifierArgs -Signature "google_host_worker.py" `
        -ProcessLabel "Google host verifier" -PidFile "google_host_worker.pid" -OutLog "google_host_worker.out.log" -ErrLog "google_host_worker.err.log"
} catch {
    Write-Warning "Google host verifier did not start: $($_.Exception.Message)"
}
