from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return code


def gpu_memory_mib() -> float | None:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            return float(result.stdout.splitlines()[0].strip())
    except Exception:
        pass
    return None


def normalize(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def main() -> int:
    manifest_path = os.getenv("SHOCKS_WHISPER_BENCHMARK_MANIFEST", "").strip()
    if not manifest_path:
        return emit({"summary": "Whisper benchmark host manifest is not configured", "configured": False}, 2)
    manifest = Path(manifest_path)
    if not manifest.is_file():
        return emit({"summary": "Whisper benchmark host manifest does not exist", "configured": False}, 2)
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:
        return emit({"summary": "faster-whisper runtime is unavailable", "error": str(exc)}, 2)

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    cases = payload.get("cases", []) if isinstance(payload, dict) else []
    if not cases:
        return emit({"summary": "Whisper benchmark manifest has no cases"}, 2)

    models = [m.strip() for m in os.getenv("SHOCKS_WHISPER_MODELS", "small,medium").split(",") if m.strip()]
    compute_types = [c.strip() for c in os.getenv("SHOCKS_WHISPER_COMPUTE_TYPES", "float16,int8_float16").split(",") if c.strip()]
    results: list[dict[str, Any]] = []
    for model_id in models:
        for compute_type in compute_types:
            started = time.monotonic()
            before_vram = gpu_memory_mib()
            try:
                model = WhisperModel(model_id, device="cuda", compute_type=compute_type)
                loaded_vram = gpu_memory_mib()
                total_audio_seconds = 0.0
                term_hits = 0
                term_total = 0
                case_results = []
                for case in cases:
                    media = Path(str(case.get("media_path", "")))
                    if not media.is_file():
                        raise RuntimeError(f"benchmark media missing: {media.name}")
                    case_started = time.monotonic()
                    segments, info = model.transcribe(str(media), language=case.get("language") or None, word_timestamps=True)
                    materialized = list(segments)
                    elapsed = time.monotonic() - case_started
                    text = " ".join(segment.text.strip() for segment in materialized).strip()
                    expected_terms = [normalize(str(term)) for term in case.get("expected_terms", []) if str(term).strip()]
                    normalized_text = normalize(text)
                    hits = sum(1 for term in expected_terms if term and term in normalized_text)
                    term_hits += hits
                    term_total += len(expected_terms)
                    duration = float(getattr(info, "duration", 0.0) or 0.0)
                    total_audio_seconds += duration
                    case_results.append({
                        "case_id": str(case.get("id") or media.name),
                        "duration_seconds": round(duration, 3),
                        "runtime_seconds": round(elapsed, 3),
                        "term_hits": hits,
                        "term_total": len(expected_terms),
                        "segment_count": len(materialized),
                    })
                runtime = time.monotonic() - started
                results.append({
                    "model": model_id,
                    "compute_type": compute_type,
                    "runtime_seconds": round(runtime, 3),
                    "audio_seconds": round(total_audio_seconds, 3),
                    "realtime_factor": round(runtime / total_audio_seconds, 4) if total_audio_seconds else None,
                    "project_term_accuracy": round(term_hits / term_total, 4) if term_total else None,
                    "gpu_memory_before_mib": before_vram,
                    "gpu_memory_loaded_mib": loaded_vram,
                    "gpu_memory_delta_mib": round(loaded_vram - before_vram, 1) if loaded_vram is not None and before_vram is not None else None,
                    "cases": case_results,
                })
                del model
            except Exception as exc:
                results.append({"model": model_id, "compute_type": compute_type, "error": str(exc)})

    successful = [r for r in results if "error" not in r]
    if not successful:
        return emit({"summary": "All Whisper benchmark configurations failed", "results": results}, 1)
    successful.sort(key=lambda r: (-(r.get("project_term_accuracy") or 0.0), r.get("realtime_factor") or 9999.0))
    best = successful[0]
    return emit({
        "summary": f"Whisper benchmark completed; best measured configuration {best['model']} / {best['compute_type']}",
        "configured": True,
        "best": best,
        "results": results,
    })


if __name__ == "__main__":
    raise SystemExit(main())
