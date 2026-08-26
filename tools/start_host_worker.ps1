. (Join-Path $PSScriptRoot "app_contract.ps1")

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
function Get-PythonLaunch {
    $VenvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $VenvPython) { return @{ FilePath = $VenvPython; PrefixArgs = @() } }
    if (Get-Command py -ErrorAction SilentlyContinue) { return @{ FilePath = "py"; PrefixArgs = @("-3") } }
    if (Get-Command python -ErrorAction SilentlyContinue) { return @{ FilePath = "python"; PrefixArgs = @() } }
    throw "No usable Python launcher found for Google host worker."
}

Import-ShocksHostEnvironment
if (-not $env:SHOCKS_GOOGLE_SPREADSHEET_ID) { Write-Host "Shock's Art GPT Bridge is not configured; host worker remains disabled."; exit 0 }
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
