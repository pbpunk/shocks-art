. (Join-Path $PSScriptRoot "app_contract.ps1")

function Get-IndexerPython {
    $VenvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $VenvPython) { return @{ FilePath = $VenvPython; PrefixArgs = @() } }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        try { & py -3.13 -c "import sys" *> $null; if ($LASTEXITCODE -eq 0) { return @{ FilePath = "py"; PrefixArgs = @("-3.13") } } } catch {}
        return @{ FilePath = "py"; PrefixArgs = @("-3") }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) { return @{ FilePath = "python"; PrefixArgs = @() } }
    throw "No usable Python launcher found for Library indexer."
}

Set-Location -LiteralPath $ProjectDir
New-Item -ItemType Directory -Force -Path $DataDir, $LogsDir | Out-Null
$PidPath = Join-Path $DataDir "indexer_worker.pid"
$Signature = "app.indexing.worker"

if (Test-Path -LiteralPath $PidPath) {
    $ExistingPid = 0
    [void][int]::TryParse((Get-Content -LiteralPath $PidPath -Raw).Trim(), [ref]$ExistingPid)
    if ($ExistingPid -gt 0) {
        $Existing = Get-CimInstance Win32_Process -Filter "ProcessId=$ExistingPid" -ErrorAction SilentlyContinue
        if ($Existing -and $Existing.CommandLine -match [regex]::Escape($Signature)) {
            Write-Host "Library indexer is already running (PID $ExistingPid)."
            exit 0
        }
    }
    Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
}

$Python = Get-IndexerPython
$Arguments = @($Python.PrefixArgs) + @("-m", "app.indexing.worker")
$Worker = Start-Process -FilePath $Python.FilePath -ArgumentList $Arguments -WorkingDirectory $ProjectDir -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $LogsDir "indexer_worker.out.log") `
    -RedirectStandardError (Join-Path $LogsDir "indexer_worker.err.log")
Set-Content -LiteralPath $PidPath -Value $Worker.Id -Encoding ascii
Start-Sleep -Seconds 2
if (-not (Get-Process -Id $Worker.Id -ErrorAction SilentlyContinue)) {
    Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
    throw "Library indexer exited during startup. Inspect $(Join-Path $LogsDir 'indexer_worker.err.log')"
}
Write-Host "Started Library indexer PID $($Worker.Id)."