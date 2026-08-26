. (Join-Path $PSScriptRoot "app_contract.ps1")

try { & (Join-Path $PSScriptRoot "stop_indexer_worker.ps1") }
catch { Write-Warning "Library indexer stop failed: $($_.Exception.Message)" }

if ($env:SHOCKS_KEEP_HOST_BRIDGE -ne "1") {
    try { & (Join-Path $PSScriptRoot "stop_host_worker.ps1") }
    catch { Write-Warning "Autonomous host bridge stop failed: $($_.Exception.Message)" }
}
Set-Location -LiteralPath $ProjectDir
$Listeners = @(Get-PortListenerProcesses)
if ($Listeners.Count -eq 0) { Remove-RuntimeMetadata; Write-Host "$AppName is already stopped."; exit 0 }
$RuntimePid = Get-RuntimeProcessId
foreach ($Listener in $Listeners) {
    if (-not (Test-OwnedServerProcess $Listener)) { throw "Refusing to stop PID $($Listener.ProcessId) on port $Port because it is not an owned $AppName server: $($Listener.CommandLine)" }
    if ($RuntimePid -and $RuntimePid -ne [int]$Listener.ProcessId) { throw "Runtime metadata says PID $RuntimePid, but port $Port is owned by PID $($Listener.ProcessId). Refusing an ambiguous stop." }
}
foreach ($Listener in $Listeners) { Write-Host "Stopping $AppName PID $($Listener.ProcessId)"; Stop-Process -Id $Listener.ProcessId -Force -ErrorAction Stop }
$Stopwatch = [Diagnostics.Stopwatch]::StartNew()
while ($Stopwatch.Elapsed.TotalSeconds -lt 10) {
    if (@(Get-PortListenerProcesses).Count -eq 0) { Remove-RuntimeMetadata; Write-Host "$AppName stopped."; exit 0 }
    Start-Sleep -Milliseconds 300
}
throw "Port $Port is still occupied after stopping $AppName."