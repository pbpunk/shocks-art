. (Join-Path $PSScriptRoot "app_contract.ps1")

function Get-IndexerPython {
    $VenvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $VenvPython) { return $VenvPython }

    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($Version in @("-3.13", "-3")) {
            try {
                $Resolved = (& py $Version -c "import sys; print(sys.executable)" 2>$null | Select-Object -First 1).Trim()
                if ($LASTEXITCODE -eq 0 -and $Resolved -and (Test-Path -LiteralPath $Resolved)) {
                    return $Resolved
                }
            } catch {}
        }
    }

    $Python = Get-Command python -ErrorAction SilentlyContinue
    if ($Python) { return $Python.Source }
    throw "No usable Python executable found for Library indexer."
}

function Get-RunningIndexerProcesses {
    @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -and $_.CommandLine -match [regex]::Escape("app.indexing.worker")
    })
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
            return
        }
    }
    Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
}

$Running = Get-RunningIndexerProcesses
if ($Running.Count -eq 1) {
    Set-Content -LiteralPath $PidPath -Value $Running[0].ProcessId -Encoding ascii
    Write-Host "Adopted running Library indexer PID $($Running[0].ProcessId)."
    return
}
if ($Running.Count -gt 1) {
    $Ids = ($Running | ForEach-Object { $_.ProcessId }) -join ", "
    throw "Multiple Library indexer processes are running ($Ids); refusing ambiguous ownership."
}

$Python = Get-IndexerPython
$Arguments = @("-m", "app.indexing.worker")
$Worker = Start-Process -FilePath $Python -ArgumentList $Arguments -WorkingDirectory $ProjectDir -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $LogsDir "indexer_worker.out.log") `
    -RedirectStandardError (Join-Path $LogsDir "indexer_worker.err.log")
Set-Content -LiteralPath $PidPath -Value $Worker.Id -Encoding ascii
Start-Sleep -Seconds 2
if (-not (Get-Process -Id $Worker.Id -ErrorAction SilentlyContinue)) {
    Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
    throw "Library indexer exited during startup. Inspect $(Join-Path $LogsDir 'indexer_worker.err.log')"
}
Write-Host "Started Library indexer PID $($Worker.Id)."