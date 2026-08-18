$ErrorActionPreference = "Stop"

$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RuntimeConfigPath = Join-Path $ProjectDir "config\qwen-indexing-runtime.json"
if (-not (Test-Path -LiteralPath $RuntimeConfigPath)) {
    throw "Missing Qwen indexing runtime configuration: $RuntimeConfigPath"
}
$RuntimeConfig = Get-Content -LiteralPath $RuntimeConfigPath -Raw | ConvertFrom-Json

$RuntimeRoot = Join-Path $ProjectDir ([string]$RuntimeConfig.runtimeRoot)
$VenvDir = Join-Path $RuntimeRoot ".venv"
$QwenRepo = Join-Path $RuntimeRoot "Qwen3-VL-Embedding"
$ModelDir = Join-Path (Join-Path $RuntimeRoot "models") ([string]$RuntimeConfig.model.directoryName)
$AppPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$IndexPython = Join-Path $VenvDir "Scripts\python.exe"
$FreezePath = Join-Path $RuntimeRoot "environment.freeze.txt"

$TorchVersion = [string]$RuntimeConfig.torch.version
$TorchvisionVersion = [string]$RuntimeConfig.torch.torchvisionVersion
$TorchIndexUrl = [string]$RuntimeConfig.torch.indexUrl
$QwenRepository = [string]$RuntimeConfig.qwenSource.repository
$QwenCommit = [string]$RuntimeConfig.qwenSource.commit
$ModelId = [string]$RuntimeConfig.model.id
$ModelRevision = [string]$RuntimeConfig.model.revision

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message"
}

function Test-PythonVersion {
    param(
        [string]$FilePath,
        [string[]]$PrefixArgs = @()
    )
    try {
        & $FilePath @PrefixArgs -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Get-BootstrapPython {
    if (Test-Path -LiteralPath $AppPython) {
        if (-not (Test-PythonVersion -FilePath $AppPython)) {
            throw "Existing app interpreter at $AppPython is older than Python 3.11 and cannot bootstrap Qwen indexing."
        }
        return @{ FilePath = $AppPython; PrefixArgs = @(); Label = $AppPython }
    }

    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($Version in @("3.13", "3.12", "3.11")) {
            $Args = @("-$Version")
            if (Test-PythonVersion -FilePath "py" -PrefixArgs $Args) {
                return @{ FilePath = "py"; PrefixArgs = $Args; Label = "py -$Version" }
            }
        }
        if (Test-PythonVersion -FilePath "py" -PrefixArgs @("-3")) {
            return @{ FilePath = "py"; PrefixArgs = @("-3"); Label = "py -3" }
        }
    }

    if (Get-Command python -ErrorAction SilentlyContinue) {
        if (Test-PythonVersion -FilePath "python") {
            return @{ FilePath = "python"; PrefixArgs = @(); Label = "python" }
        }
    }

    throw "No Python 3.11+ interpreter is available to create the isolated Qwen indexing environment. Existing app .venv is optional. This script will not modify system Python."
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git is required to fetch the official Qwen3-VL-Embedding implementation."
}

New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null

if (-not (Test-Path -LiteralPath $IndexPython)) {
    $Bootstrap = Get-BootstrapPython
    Write-Step "Creating isolated indexing virtual environment with $($Bootstrap.Label)"
    $BootstrapFilePath = [string]$Bootstrap.FilePath
    $BootstrapArgs = @($Bootstrap.PrefixArgs)
    & $BootstrapFilePath @BootstrapArgs -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { throw "Could not create isolated indexing virtual environment with $($Bootstrap.Label)." }
}

if (-not (Test-Path -LiteralPath $IndexPython)) {
    throw "Isolated indexing interpreter was not created at $IndexPython."
}
if (-not (Test-PythonVersion -FilePath $IndexPython)) {
    throw "Isolated indexing interpreter at $IndexPython is older than Python 3.11. Delete data\qwen_indexing\.venv and rerun setup with a supported bootstrap interpreter."
}

Write-Step "Upgrading isolated packaging tools"
& $IndexPython -m pip install --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) { throw "Could not update pip/setuptools/wheel inside the indexing environment." }

Write-Step "Installing pinned PyTorch $TorchVersion CUDA runtime"
& $IndexPython -m pip install "torch==$TorchVersion" "torchvision==$TorchvisionVersion" --index-url $TorchIndexUrl
if ($LASTEXITCODE -ne 0) {
    throw "Pinned PyTorch installation failed. The existing app environment was not modified."
}

if (-not (Test-Path -LiteralPath (Join-Path $QwenRepo ".git"))) {
    Write-Step "Cloning official Qwen3-VL-Embedding implementation"
    & git clone --no-checkout $QwenRepository $QwenRepo
    if ($LASTEXITCODE -ne 0) { throw "Could not clone Qwen3-VL-Embedding." }
}

Write-Step "Pinning Qwen3-VL-Embedding source to $QwenCommit"
& git -C $QwenRepo fetch origin $QwenCommit --depth 1
if ($LASTEXITCODE -ne 0) { throw "Could not fetch pinned Qwen source commit $QwenCommit." }
& git -C $QwenRepo checkout --detach --force $QwenCommit
if ($LASTEXITCODE -ne 0) { throw "Could not check out pinned Qwen source commit $QwenCommit." }
$ResolvedQwenCommit = (& git -C $QwenRepo rev-parse HEAD).Trim()
if ($ResolvedQwenCommit -ne $QwenCommit) {
    throw "Qwen source pin mismatch: expected $QwenCommit, got $ResolvedQwenCommit."
}

Write-Step "Installing pinned Qwen source dependencies into the isolated environment"
& $IndexPython -m pip install -e $QwenRepo
if ($LASTEXITCODE -ne 0) {
    throw "Qwen dependency installation failed. The existing app environment was not modified."
}

& $IndexPython -m pip install huggingface-hub
if ($LASTEXITCODE -ne 0) { throw "Could not install huggingface-hub in the indexing environment." }

if (-not (Test-Path -LiteralPath (Join-Path $ModelDir "config.json"))) {
    Write-Step "Downloading pinned $ModelId model snapshot"
    New-Item -ItemType Directory -Force -Path $ModelDir | Out-Null
    $env:SHOCKS_QWEN_MODEL_DIR = $ModelDir
    $env:SHOCKS_QWEN_MODEL_ID = $ModelId
    $env:SHOCKS_QWEN_MODEL_REVISION = $ModelRevision
    & $IndexPython -c "import os; from huggingface_hub import snapshot_download; snapshot_download(repo_id=os.environ['SHOCKS_QWEN_MODEL_ID'], revision=os.environ['SHOCKS_QWEN_MODEL_REVISION'], local_dir=os.environ['SHOCKS_QWEN_MODEL_DIR'])"
    if ($LASTEXITCODE -ne 0) { throw "Could not download pinned Qwen3-VL-Embedding-2B model revision $ModelRevision." }
}

Write-Step "Recording resolved isolated package environment"
& $IndexPython -m pip freeze | Set-Content -LiteralPath $FreezePath -Encoding UTF8
if ($LASTEXITCODE -ne 0) { throw "Could not record isolated package environment." }

Write-Step "Reporting isolated runtime"
& $IndexPython -c "import sys, torch; print('python=' + sys.version.split()[0]); print('torch=' + torch.__version__); print('torch_cuda=' + str(torch.version.cuda)); print('cuda_available=' + str(torch.cuda.is_available())); print('gpu=' + (torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'none'))"

Write-Host ""
Write-Host "Isolated indexing environment ready."
Write-Host "Python: $IndexPython"
Write-Host "Qwen source commit: $QwenCommit"
Write-Host "Model revision: $ModelRevision"
Write-Host "Resolved packages: $FreezePath"
Write-Host "Benchmark:"
Write-Host ('  "{0}" "{1}" --qwen-repo "{2}" --model-dir "{3}"' -f $IndexPython, (Join-Path $ProjectDir "tools\benchmark_qwen_embedding.py"), $QwenRepo, $ModelDir)
