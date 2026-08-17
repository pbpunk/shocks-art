. (Join-Path $PSScriptRoot "app_contract.ps1")

Write-Step "Stopping $AppName"
& (Join-Path $PSScriptRoot "stop_app.ps1")

Write-Step "Starting $AppName"
& (Join-Path $PSScriptRoot "start_app.ps1")
