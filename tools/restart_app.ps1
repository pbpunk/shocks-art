. (Join-Path $PSScriptRoot "app_contract.ps1")

Write-Step "Stopping $AppName while preserving autonomous host bridge"
$PreviousKeepBridge = $env:SHOCKS_KEEP_HOST_BRIDGE
$env:SHOCKS_KEEP_HOST_BRIDGE = "1"
try {
    & (Join-Path $PSScriptRoot "stop_app.ps1")
} finally {
    if ($null -eq $PreviousKeepBridge) { Remove-Item Env:SHOCKS_KEEP_HOST_BRIDGE -ErrorAction SilentlyContinue }
    else { $env:SHOCKS_KEEP_HOST_BRIDGE = $PreviousKeepBridge }
}
Write-Step "Starting $AppName"
& (Join-Path $PSScriptRoot "start_app.ps1")
