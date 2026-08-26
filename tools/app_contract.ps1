$ErrorActionPreference = "Stop"

$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ManifestPath = Join-Path $ProjectDir "jarvis.app.json"
if (-not (Test-Path -LiteralPath $ManifestPath)) {
    throw "Missing JARVIS app manifest: $ManifestPath"
}

$Manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
$AppId = [string]$Manifest.id
$AppName = [string]$Manifest.name
$Route = [string]$Manifest.route
$Port = [int]$Manifest.primaryPort
$LocalProtocol = if ($Manifest.local.protocol) { [string]$Manifest.local.protocol } else { "http" }
$LocalBaseUrl = if ($Manifest.local.baseUrl) { [string]$Manifest.local.baseUrl } else { "${LocalProtocol}://127.0.0.1:$Port$Route" }
$HealthUrl = if ($Manifest.health.url) { [string]$Manifest.health.url } else { "$LocalBaseUrl/health" }
$StartupTimeoutSeconds = if ($Manifest.health.startupTimeoutSeconds) { [int]$Manifest.health.startupTimeoutSeconds } else { 45 }
$RuntimeRelativePath = if ($Manifest.runtime.file) { [string]$Manifest.runtime.file } else { "data/runtime.json" }
$LogsRelativePath = if ($Manifest.runtime.logsDir) { [string]$Manifest.runtime.logsDir } else { "data/logs" }
$DataDir = Join-Path $ProjectDir "data"
$RuntimePath = Join-Path $ProjectDir $RuntimeRelativePath
$LogsDir = Join-Path $ProjectDir $LogsRelativePath
$PublicBaseUrl = "https://desktop.tail27cee7.ts.net"

if (-not $AppId -or -not $AppName -or -not $Route -or $Port -le 0) {
    throw "jarvis.app.json is missing required id/name/route/primaryPort values."
}
if ($Route -eq "/") {
    throw "$AppName may not claim the JARVIS root route."
}
if (-not (@($Manifest.ports | ForEach-Object { [int]$_ }) -contains $Port)) {
    throw "jarvis.app.json primaryPort $Port is not declared in ports."
}

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message"
}

function Get-GitRevision {
    try {
        $Commit = (& git -C $ProjectDir rev-parse --short HEAD 2>$null).Trim()
        if ($LASTEXITCODE -eq 0 -and $Commit) { return $Commit }
    } catch {}
    return "unknown"
}

function Get-PortListenerProcesses {
    try {
        $Ids = @(
            Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
                Select-Object -ExpandProperty OwningProcess -Unique
        )
    } catch {
        return @()
    }
    return @(
        $Ids |
            ForEach-Object { Get-CimInstance Win32_Process -Filter "ProcessId=$_" -ErrorAction SilentlyContinue } |
            Where-Object { $_ }
    )
}

function Get-RuntimeProcessId {
    if (-not (Test-Path -LiteralPath $RuntimePath)) { return $null }
    try {
        $Runtime = Get-Content -LiteralPath $RuntimePath -Raw | ConvertFrom-Json
        if ($Runtime.app -eq $AppId -and $Runtime.pid) { return [int]$Runtime.pid }
    } catch {}
    return $null
}

function Get-HealthPayload {
    param([int]$TimeoutSeconds = 4)
    try {
        return Invoke-RestMethod -UseBasicParsing -Uri $HealthUrl -TimeoutSec $TimeoutSeconds
    } catch {
        return $null
    }
}

function Test-AppIdentity {
    param([int]$TimeoutSeconds = 4)
    $Payload = Get-HealthPayload -TimeoutSeconds $TimeoutSeconds
    if (-not $Payload) { return $false }
    if ([string]$Payload.app -ne $AppId) { return $false }
    if ($Payload.route -and [string]$Payload.route -ne $Route) { return $false }
    return $true
}

function Test-OwnedServerProcess {
    param($Process)
    if (-not $Process -or -not $Process.CommandLine) { return $false }

    $CommandLine = [string]$Process.CommandLine
    $HasServerSignature = $CommandLine -match '(-m\s+uvicorn|uvicorn(?:\.exe)?)\s+app\.main:app'
    $HasPort = $CommandLine -match "(?:--port\s+|--port=)$Port(?:\s|$)"
    if (-not ($HasServerSignature -and $HasPort)) { return $false }

    $RuntimePid = Get-RuntimeProcessId
    if ($RuntimePid -and $RuntimePid -eq [int]$Process.ProcessId) { return $true }
    if ($CommandLine.IndexOf($ProjectDir, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) { return $true }

    # Migration compatibility: the previous launcher did not put the project path
    # on the command line, so a healthy app identity is the ownership proof.
    return Test-AppIdentity -TimeoutSeconds 2
}

function Test-HttpEndpoint {
    param(
        [string]$Url,
        [int]$TimeoutSeconds = 4
    )

    if ($Url.StartsWith("https://", [System.StringComparison]::OrdinalIgnoreCase)) {
        try {
            & curl.exe -k --silent --show-error --fail --max-time $TimeoutSeconds $Url *> $null
            return $LASTEXITCODE -eq 0
        } catch {
            return $false
        }
    }

    try {
        $Response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec $TimeoutSeconds
        return $Response.StatusCode -ge 200 -and $Response.StatusCode -lt 300
    } catch {
        return $false
    }
}

function Get-TailscaleRouteProperty {
    param(
        $Object,
        [string]$Name
    )
    if (-not $Object) { return $null }
    $Property = $Object.PSObject.Properties[$Name]
    if (-not $Property) { return $null }
    return $Property.Value
}

function Get-TailscaleStatus {
    param([string]$Command = "funnel")
    try {
        $Json = & tailscale.exe $Command status --json 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $Json) { return $null }
        return ($Json | ConvertFrom-Json)
    } catch {
        return $null
    }
}

function Test-TailscaleRouteContract {
    param(
        [Parameter(Mandatory = $true)]$Status,
        [int]$HttpsPort,
        [string]$Path,
        [string]$Target,
        [switch]$RequireFunnel,
        [switch]$RequirePrivate
    )

    $HostName = ([Uri]$PublicBaseUrl).Host
    $ListenerKey = "$HostName`:$HttpsPort"
    $Listener = Get-TailscaleRouteProperty -Object $Status.Web -Name $ListenerKey
    if (-not $Listener -and $Status.Web) {
        $Listener = @(
            $Status.Web.PSObject.Properties |
                Where-Object { $_.Name -like "*:$HttpsPort" } |
                Select-Object -First 1 -ExpandProperty Value
        )[0]
    }

    $Handler = $null
    if ($Listener) {
        $Handler = Get-TailscaleRouteProperty -Object $Listener.Handlers -Name $Path
    }

    $RoutePresent = $null -ne $Handler
    $ActualTarget = if ($Handler -and $Handler.Proxy) { [string]$Handler.Proxy } else { "" }
    $TargetMatches = $RoutePresent -and $ActualTarget -eq $Target
    $AllowFunnel = Get-TailscaleRouteProperty -Object $Status.AllowFunnel -Name $ListenerKey
    $IsFunnel = $AllowFunnel -eq $true
    $Mode = if ($IsFunnel) { "Funnel/public" } else { "Serve/tailnet-only" }

    $Ok = $RoutePresent -and $TargetMatches
    if ($RequireFunnel) { $Ok = $Ok -and $IsFunnel }
    if ($RequirePrivate) { $Ok = $Ok -and -not $IsFunnel }

    $Problems = @()
    if (-not $RoutePresent) {
        $Problems += "$Path is missing on HTTPS $HttpsPort"
    } elseif (-not $TargetMatches) {
        $Problems += "$Path points to '$ActualTarget' instead of '$Target'"
    }
    if ($RequireFunnel -and -not $IsFunnel) {
        $Problems += "HTTPS $HttpsPort has been switched back to Serve/tailnet-only; run Funnel for the shared 443 listener"
    }
    if ($RequirePrivate -and $IsFunnel) {
        $Problems += "HTTPS $HttpsPort is Funnel/public but should remain private Serve"
    }

    return [pscustomobject]@{
        Ok = [bool]$Ok
        Mode = $Mode
        ListenerKey = $ListenerKey
        RoutePresent = [bool]$RoutePresent
        TargetMatches = [bool]$TargetMatches
        Target = $ActualTarget
        Message = ($Problems -join "; ")
    }
}

function Wait-AppHealth {
    param([int]$TimeoutSeconds = $StartupTimeoutSeconds)
    $Stopwatch = [Diagnostics.Stopwatch]::StartNew()
    while ($Stopwatch.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        if (Test-AppIdentity -TimeoutSeconds 3) { return $true }
        Start-Sleep -Milliseconds 750
    }
    return $false
}

function Write-RuntimeMetadata {
    param([int]$ProcessId)
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $RuntimePath) | Out-Null
    $Runtime = [ordered]@{
        schemaVersion = 1
        app = $AppId
        pid = $ProcessId
        ports = @($Manifest.ports | ForEach-Object { [int]$_ })
        route = $Route
        startedAt = (Get-Date).ToUniversalTime().ToString("o")
        commit = Get-GitRevision
        mode = "production"
    }
    $Runtime | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $RuntimePath -Encoding UTF8
}

function Remove-RuntimeMetadata {
    Remove-Item -LiteralPath $RuntimePath -Force -ErrorAction SilentlyContinue
}

function Get-TailscaleUpstream {
    if ($Manifest.network.upstream) { return [string]$Manifest.network.upstream }
    if ($LocalProtocol -eq "https") { return "https+insecure://127.0.0.1:$Port$Route" }
    return "http://127.0.0.1:$Port$Route"
}

function Set-AppTailscaleRoutes {
    $Target = Get-TailscaleUpstream
    $PublicPort = if ($Manifest.network.public.httpsPort) { [int]$Manifest.network.public.httpsPort } else { 443 }
    $PrivatePort = if ($Manifest.network.private.httpsPort) { [int]$Manifest.network.private.httpsPort } else { 8443 }
    $PublicPath = if ($Manifest.network.public.path) { [string]$Manifest.network.public.path } else { $Route }
    $PrivatePath = if ($Manifest.network.private.path) { [string]$Manifest.network.private.path } else { $Route }

    if ($PublicPath -eq "/" -or $PrivatePath -eq "/") {
        throw "$AppName may not claim the Tailscale root route. JARVIS-dashboard owns /."
    }
    if ($PublicPath -ne $Route -or $PrivatePath -ne $Route) {
        throw "Tailscale route paths must match the app-owned namespace $Route."
    }

    $PublicOk = $false
    $PrivateOk = $false
    try {
        & tailscale.exe funnel --https=$PublicPort --set-path=$PublicPath --bg --yes $Target
        $PublicOk = $LASTEXITCODE -eq 0
        if (-not $PublicOk) { Write-Warning "Could not configure public Funnel route. Local app remains healthy." }
    } catch {
        Write-Warning "Could not configure public Funnel route: $($_.Exception.Message). Local app remains healthy."
    }
    try {
        & tailscale.exe serve --https=$PrivatePort --set-path=$PrivatePath --bg --yes $Target
        $PrivateOk = $LASTEXITCODE -eq 0
        if (-not $PrivateOk) { Write-Warning "Could not configure private Serve route. Local app remains healthy." }
    } catch {
        Write-Warning "Could not configure private Serve route: $($_.Exception.Message). Local app remains healthy."
    }

    $Status = Get-TailscaleStatus -Command "funnel"
    if ($Status) {
        $PublicRoute = Test-TailscaleRouteContract -Status $Status -HttpsPort $PublicPort -Path $PublicPath -Target $Target -RequireFunnel
        if (-not $PublicRoute.Ok) {
            $PublicOk = $false
            Write-Warning "Public Funnel verification failed: $($PublicRoute.Message). Local app remains healthy."
        }

        $PrivateRoute = Test-TailscaleRouteContract -Status $Status -HttpsPort $PrivatePort -Path $PrivatePath -Target $Target -RequirePrivate
        if (-not $PrivateRoute.Ok) {
            $PrivateOk = $false
            Write-Warning "Private Serve verification failed: $($PrivateRoute.Message). Local app remains healthy."
        }
    } else {
        Write-Warning "Could not inspect Tailscale Serve/Funnel status. Local app remains healthy."
    }

    return @{ PublicOk = $PublicOk; PrivateOk = $PrivateOk; Target = $Target }
}

function Test-PublicContract {
    param([switch]$WarnOnly)
    $PublicPort = if ($Manifest.network.public.httpsPort) { [int]$Manifest.network.public.httpsPort } else { 443 }
    $PublicPath = if ($Manifest.network.public.path) { [string]$Manifest.network.public.path } else { $Route }
    $Target = Get-TailscaleUpstream
    $Status = Get-TailscaleStatus -Command "funnel"
    if (-not $Status) {
        $Message = "Public verification failed: could not inspect Tailscale Serve/Funnel status for HTTPS $PublicPort"
        if ($WarnOnly) { Write-Warning $Message; return $false }
        throw $Message
    }

    $PublicRoute = Test-TailscaleRouteContract -Status $Status -HttpsPort $PublicPort -Path $PublicPath -Target $Target -RequireFunnel
    if (-not $PublicRoute.Ok) {
        $Message = "Public verification failed: $($PublicRoute.Message)"
        if ($WarnOnly) { Write-Warning $Message; return $false }
        throw $Message
    }

    $Checks = @(
        @{ Name = "HTML"; Url = "$PublicBaseUrl$Route/" },
        @{ Name = "health"; Url = "$PublicBaseUrl$Route/health" },
        @{ Name = "API"; Url = "$PublicBaseUrl$Route/api/ping" }
    )
    $Failures = @()
    foreach ($Check in $Checks) {
        if (-not (Test-HttpEndpoint -Url $Check.Url -TimeoutSeconds 6)) {
            $Failures += "$($Check.Name): $($Check.Url)"
        }
    }
    if ($Failures.Count -gt 0) {
        $Message = "Public verification failed: " + ($Failures -join "; ")
        if ($WarnOnly) { Write-Warning $Message; return $false }
        throw $Message
    }
    return $true
}
