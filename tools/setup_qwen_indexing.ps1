$ErrorActionPreference = "Stop"

$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RuntimeRoot = Join-Path $ProjectDir "data\qwen_indexing"
$VenvDir = Join-Path $RuntimeRoot ".venv"
$QwenRepo = Join-Path $RuntimeRoot "Qwen3-VL-Embedding"
$ModelDir = Join-Path $RuntimeRoot "models\Qwen3-VL-Embedding-2B"
$AppPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$IndexPython = Join-Path $VenvDir "Scripts\python.exe"

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message"
}

if (-not (Test-Path -LiteralPath $AppPython)) {
    throw "Expected the existing app interpreter at $AppPython. Run the normal app setup first."
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git is required to fetch the official Qwen3-VL-Embedding implementation."
}

New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null

if (-not (Test-Path -LiteralPath $IndexPython)) {
    Write-Step "Creating isolated indexing virtual environment"
    & $AppPython -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { throw "Could not create isolated indexing virtual environment." }
}

Write-Step "Upgrading isolated packaging tools"
& $IndexPython -m pip install --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) { throw "Could not update pip/setuptools/wheel inside the indexing environment." }

Write-Step "Installing PyTorch 2.8 CUDA 12.8 runtime in the isolated environment"
& $IndexPython -m pip install torch==2.8.0 torchvision==0.23.0 --index-url https://download.pytorch.org/whl/cu128
if ($LASTEXITCODE -ne 0) {
    throw "PyTorch 2.8 CUDA 12.8 installation failed. The existing app environment was not modified."
}

if (-not (Test-Path -LiteralPath (Join-Path $QwenRepo ".git"))) {
    Write-Step "Cloning official Qwen3-VL-Embedding implementation"
    & git clone --depth 1 https://github.com/QwenLM/Qwen3-VL-Embedding.git $QwenRepo
    if ($LASTEXITCODE -ne 0) { throw "Could not clone Qwen3-VL-Embedding." }
} else {
    Write-Step "Refreshing existing Qwen3-VL-Embedding checkout"
    & git -C $QwenRepo fetch origin main --depth 1
    if ($LASTEXITCODE -ne 0) { throw "Could not fetch Qwen3-VL-Embedding main." }
    & git -C $QwenRepo reset --hard origin/main
    if ($LASTEXITCODE -ne 0) { throw "Could not reset Qwen3-VL-Embedding checkout to origin/main." }
}

Write-Step "Installing official Qwen dependencies into the isolated environment"
& $IndexPython -m pip install -e $QwenRepo
if ($LASTEXITCODE -ne 0) {
    throw "Qwen dependency installation failed. The existing app environment was not modified."
}

& $IndexPython -m pip install huggingface-hub
if ($LASTEXITCODE -ne 0) { throw "Could not install huggingface-hub in the indexing environment." }

if (-not (Test-Path -LiteralPath (Join-Path $ModelDir "config.json"))) {
    Write-Step "Downloading Qwen3-VL-Embedding-2B model"
    New-Item -ItemType Directory -Force -Path $ModelDir | Out-Null
    $env:SHOCKS_QWEN_MODEL_DIR = $ModelDir
    & $IndexPython -c "import os; from huggingface_hub import snapshot_download; snapshot_download(repo_id='Qwen/Qwen3-VL-Embedding-2B', local_dir=os.environ['SHOCKS_QWEN_MODEL_DIR'])"
    if ($LASTEXITCODE -ne 0) { throw "Could not download Qwen3-VL-Embedding-2B." }
}

Write-Step "Reporting isolated runtime"
& $IndexPython -c "import sys, torch; print('python=' + sys.version.split()[0]); print('torch=' + torch.__version__); print('torch_cuda=' + str(torch.version.cuda)); print('cuda_available=' + str(torch.cuda.is_available())); print('gpu=' + (torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'none'))"

Write-Host ""
Write-Host "Isolated indexing environment ready."
Write-Host "Python: $IndexPython"
Write-Host "Qwen repo: $QwenRepo"
Write-Host "Model: $ModelDir"
Write-Host "Benchmark:"
Write-Host ('  "{0}" "{1}" --qwen-repo "{2}" --model-dir "{3}"' -f $IndexPython, (Join-Path $ProjectDir "tools\benchmark_qwen_embedding.py"), $QwenRepo, $ModelDir)
