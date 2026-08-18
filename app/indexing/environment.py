from __future__ import annotations

import importlib.metadata
import json
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone


PACKAGE_NAMES = (
    "torch",
    "torchvision",
    "transformers",
    "accelerate",
    "decord",
    "qwen-vl-utils",
    "faster-whisper",
    "ctranslate2",
)


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _run(command: list[str], *, timeout: int = 15) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _nvidia_report() -> dict:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return {"available": False, "gpus": [], "reportedCudaVersion": None}

    query = _run(
        [
            executable,
            "--query-gpu=name,driver_version,memory.total",
            "--format=csv,noheader,nounits",
        ]
    )
    gpus: list[dict] = []
    if query and query.returncode == 0:
        for line in (query.stdout or "").splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) != 3:
                continue
            try:
                memory_mib = int(float(parts[2]))
            except ValueError:
                memory_mib = None
            gpus.append(
                {
                    "name": parts[0],
                    "driverVersion": parts[1],
                    "memoryTotalMiB": memory_mib,
                }
            )

    full = _run([executable])
    cuda_version = None
    if full and full.returncode == 0:
        match = re.search(r"CUDA Version:\s*([0-9.]+)", full.stdout or "")
        if match:
            cuda_version = match.group(1)

    return {
        "available": True,
        "gpus": gpus,
        "reportedCudaVersion": cuda_version,
    }


def _nvcc_report() -> dict:
    executable = shutil.which("nvcc")
    if not executable:
        return {"available": False, "release": None}
    result = _run([executable, "--version"])
    release = None
    if result and result.returncode == 0:
        match = re.search(r"release\s+([0-9.]+)", result.stdout or "", flags=re.IGNORECASE)
        if match:
            release = match.group(1)
    return {"available": True, "release": release}


def _torch_probe(torch_version: str | None) -> dict:
    if not torch_version:
        return {"installed": False, "probeSucceeded": False}

    probe = """
import json
import torch
payload = {
    'installed': True,
    'probeSucceeded': True,
    'version': torch.__version__,
    'builtCudaVersion': torch.version.cuda,
    'cudaAvailable': bool(torch.cuda.is_available()),
    'deviceCount': int(torch.cuda.device_count()) if torch.cuda.is_available() else 0,
    'deviceName': None,
    'deviceCapability': None,
    'deviceMemoryTotalMiB': None,
}
if payload['cudaAvailable'] and payload['deviceCount']:
    props = torch.cuda.get_device_properties(0)
    payload['deviceName'] = props.name
    payload['deviceCapability'] = list(torch.cuda.get_device_capability(0))
    payload['deviceMemoryTotalMiB'] = int(props.total_memory // (1024 * 1024))
print(json.dumps(payload))
""".strip()
    result = _run([sys.executable, "-c", probe], timeout=30)
    if not result or result.returncode != 0:
        detail = ""
        if result:
            detail = (result.stderr or result.stdout or "").strip()[:500]
        return {
            "installed": True,
            "probeSucceeded": False,
            "version": torch_version,
            "error": detail or "torch subprocess probe failed",
        }
    try:
        payload = json.loads((result.stdout or "").strip())
    except json.JSONDecodeError:
        return {
            "installed": True,
            "probeSucceeded": False,
            "version": torch_version,
            "error": "torch subprocess returned invalid JSON",
        }
    return payload


def collect_indexing_environment() -> dict:
    """Collect read-only workstation ML/runtime diagnostics.

    Heavy ML libraries are never imported into the calling process. When Torch
    is installed, CUDA capability is checked in a child Python process.
    """

    packages = {name: _package_version(name) for name in PACKAGE_NAMES}
    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "mutatesState": False,
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
        },
        "os": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "nvidia": _nvidia_report(),
        "nvcc": _nvcc_report(),
        "packages": packages,
        "torch": _torch_probe(packages["torch"]),
    }
