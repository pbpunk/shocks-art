. (Join-Path $PSScriptRoot "app_contract.ps1")

Set-Location -LiteralPath $ProjectDir
Write-Step "Checking repository boundary"
$GitRoot = ""
try {
    $GitRoot = (& git rev-parse --show-toplevel 2>$null).Trim()
} catch {}

if (-not $GitRoot) {
    Write-Host "No Git repository found; update will restart the current local app without pulling."
} else {
    $ResolvedGitRoot = (Resolve-Path -LiteralPath $GitRoot).Path.TrimEnd('\')
    $ResolvedProject = $ProjectDir.TrimEnd('\')
    if ($ResolvedGitRoot -ne $ResolvedProject) {
        throw "Configured app root is $ResolvedProject but Git root is $ResolvedGitRoot. Refusing to update the wrong repository."
    }

    & git diff --quiet
    if ($LASTEXITCODE -ne 0) { throw "Working tree has uncommitted changes. Reconcile them before Update App." }
    & git diff --cached --quiet
    if ($LASTEXITCODE -ne 0) { throw "Index has staged changes. Reconcile them before Update App." }

    Write-Step "Checking for updates"
    $Origin = (& git remote get-url origin 2>$null)
    if ($LASTEXITCODE -eq 0 -and $Origin) {
        & git fetch origin main --prune
        if ($LASTEXITCODE -ne 0) { throw "git fetch failed. Resolve the Git error above before retrying." }
        & git merge-base --is-ancestor HEAD origin/main
        if ($LASTEXITCODE -ne 0) {
            throw "Local HEAD is not an ancestor of origin/main. Refusing a divergent or locally-ahead update."
        }
        & git pull --ff-only origin main
        if ($LASTEXITCODE -ne 0) { throw "git pull --ff-only failed. Resolve the Git error above before retrying." }
    } else {
        Write-Host "No origin remote configured; skipping Git pull."
    }
}

$Branch = ""
try {
    $Branch = (& git branch --show-current 2>$null).Trim()
} catch {}
$Commit = Get-GitRevision
Write-Host "Checked out: $Branch $Commit"

Write-Step "Stopping the app-owned runtime"
& (Join-Path $PSScriptRoot "stop_app.ps1")

Write-Step "Starting updated runtime"
& (Join-Path $PSScriptRoot "start_app.ps1")

Write-Host "Update complete at revision $(Get-GitRevision)."
