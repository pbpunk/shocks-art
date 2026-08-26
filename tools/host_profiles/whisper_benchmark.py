from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

MIN_GENERAL_ACCURACY = 0.82
MIN_PROJECT_TERM_RECALL = 0.85
MIN_TIMESTAMP_VALIDITY = 0.99
CANDIDATES = (
    ("small.en", "float16"),
    ("medium.en", "float16"),
    ("medium.en", "int8_float16"),
)
LIVE_ROOT = Path(os.getenv("SHOCKS_HOST_LIVE_ROOT", Path(__file__).resolve().parents[2])).resolve()
DEFAULT_MANIFEST_PATH = LIVE_ROOT / "data" / "whisper_benchmark" / "manifest.json"
DEFAULT_MANIFEST_HINT = "data/whisper_benchmark/manifest.json"
SETUP_DOC = "docs/WHISPER_BENCHMARK.md"


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return code


def resolve_manifest_path() -> Path:
    configured = os.getenv("SHOCKS_WHISPER_BENCHMARK_MANIFEST", "").strip()
    if not configured:
        return DEFAULT_MANIFEST_PATH
    path = Path(configured)
    if not path.is_absolute():
        path = LIVE_ROOT / path
    return path.resolve()


def validate_manifest_payload(payload: Any) -> list[dict[str, Any]]:
    cases = payload.get("cases", payload.get("samples", [])) if isinstance(payload, dict) else []
    if not isinstance(cases, list) or not cases:
        raise ValueError("manifest must contain a non-empty cases array")

    seen_ids: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(cases, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"case {index} must be an object")
        case_id = str(raw.get("id") or f"case-{index}").strip()
        if not case_id:
            raise ValueError(f"case {index} has an empty id")
        if case_id in seen_ids:
            raise ValueError(f"duplicate case id: {case_id}")
        seen_ids.add(case_id)

        media_value = str(raw.get("media_path", raw.get("path", ""))).strip()
        if not media_value:
            raise ValueError(f"case {case_id} is missing media_path")
        reference = str(raw.get("reference_text", "")).strip()
        if not reference:
            raise ValueError(f"case {case_id} is missing reference_text")

        terms = raw.get("project_terms", raw.get("expected_terms", []))
        if not isinstance(terms, list):
            raise ValueError(f"case {case_id} project_terms must be an array")
        normalized_terms = [str(term).strip() for term in terms if str(term).strip()]
        if not normalized_terms:
            raise ValueError(f"case {case_id} must label at least one project_term")

        language = str(raw.get("language") or "en").strip() or "en"
        normalized.append(
            {
                "id": case_id,
                "media_path": media_value,
                "reference_text": reference,
                "project_terms": normalized_terms,
                "language": language,
            }
        )
    return normalized


def load_manifest(path: Path, *, require_media: bool = True) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = validate_manifest_payload(payload)
    if require_media:
        for case in cases:
            media = Path(case["media_path"])
            if not media.is_absolute():
                media = (path.parent / media).resolve()
            if not media.is_file():
                raise FileNotFoundError(f"benchmark media missing for {case['id']}: {media.name}")
            case["media_path"] = str(media)
    return cases


def gpu_memory_mib() -> float | None:
    try:
        result = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"], text=True, capture_output=True, timeout=10, check=False)
        for line in result.stdout.splitlines():
            if "," in line:
                pid, value = [part.strip() for part in line.split(",", 1)]
                if pid == str(os.getpid()):
                    return float(value)
    except Exception:
        pass
    return None


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def edit_distance(a: list[str], b: list[str]) -> int:
    row = list(range(len(b) + 1))
    for i, left in enumerate(a, 1):
        new = [i]
        for j, right in enumerate(b, 1):
            new.append(min(new[-1] + 1, row[j] + 1, row[j - 1] + (left != right)))
        row = new
    return row[-1]


def general_accuracy(reference: str, hypothesis: str) -> float:
    ref = tokens(reference)
    return 1.0 if not ref else max(0.0, 1.0 - edit_distance(ref, tokens(hypothesis)) / len(ref))


def normalized_phrase(text: str) -> str:
    return " ".join(tokens(text))


def main() -> int:
    manifest = resolve_manifest_path()
    if not manifest.is_file():
        return emit(
            {
                "summary": "Whisper benchmark manifest is not ready",
                "configured": False,
                "expected_manifest": DEFAULT_MANIFEST_HINT,
                "setup_doc": SETUP_DOC,
            },
            2,
        )
    try:
        cases = load_manifest(manifest)
    except (json.JSONDecodeError, ValueError, FileNotFoundError) as exc:
        return emit(
            {
                "summary": "Whisper benchmark manifest is invalid",
                "configured": True,
                "manifest_name": manifest.name,
                "setup_doc": SETUP_DOC,
                "error": str(exc)[:800],
            },
            2,
        )
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:
        return emit({"summary": "faster-whisper runtime is unavailable; configure SHOCKS_WHISPER_PYTHON to its isolated local runtime", "error_type": type(exc).__name__}, 2)

    results: list[dict[str, Any]] = []
    for model_id, compute_type in CANDIDATES:
        model_started = time.monotonic()
        before_vram = gpu_memory_mib() or 0.0
        try:
            model = WhisperModel(model_id, device="cuda", compute_type=compute_type)
            load_seconds = time.monotonic() - model_started
            peak_vram = max(before_vram, gpu_memory_mib() or 0.0)
            total_audio = total_runtime = 0.0
            accuracy_values: list[float] = []
            term_hits = term_total = timestamp_valid = timestamp_total = 0
            case_results = []
            for case in cases:
                media = Path(case["media_path"])
                started = time.monotonic()
                segments, info = model.transcribe(str(media), language=case["language"], word_timestamps=True, vad_filter=True)
                materialized = list(segments)
                elapsed = time.monotonic() - started
                transcript = " ".join(segment.text.strip() for segment in materialized).strip()
                reference = case["reference_text"]
                case_accuracy = general_accuracy(reference, transcript)
                accuracy_values.append(case_accuracy)
                normalized_text = normalized_phrase(transcript)
                normalized_terms = [normalized_phrase(term) for term in case["project_terms"]]
                hits = sum(1 for term in normalized_terms if term and term in normalized_text)
                term_hits += hits; term_total += len(normalized_terms)
                valid = sum(1 for segment in materialized if float(segment.start) >= 0 and float(segment.end) >= float(segment.start))
                timestamp_valid += valid; timestamp_total += len(materialized)
                duration = float(getattr(info, "duration", 0.0) or 0.0)
                total_audio += duration; total_runtime += elapsed
                peak_vram = max(peak_vram, gpu_memory_mib() or 0.0)
                case_results.append({"case_id": case["id"], "duration_seconds": round(duration, 3), "runtime_seconds": round(elapsed, 3), "general_accuracy": round(case_accuracy, 4), "project_term_hits": hits, "project_term_total": len(normalized_terms), "timestamp_valid": valid, "timestamp_total": len(materialized)})
            row = {
                "model": model_id, "compute_type": compute_type,
                "load_seconds": round(load_seconds, 3), "runtime_seconds": round(total_runtime, 3), "audio_seconds": round(total_audio, 3),
                "realtime_factor": round(total_runtime / total_audio, 4) if total_audio else None,
                "general_accuracy": round(sum(accuracy_values) / len(accuracy_values), 4),
                "project_term_recall": round(term_hits / term_total, 4) if term_total else 1.0,
                "timestamp_validity": round(timestamp_valid / timestamp_total, 4) if timestamp_total else 1.0,
                "peak_process_vram_mib": round(peak_vram, 1), "cases": case_results,
            }
            row["acceptable"] = row["general_accuracy"] >= MIN_GENERAL_ACCURACY and row["project_term_recall"] >= MIN_PROJECT_TERM_RECALL and row["timestamp_validity"] >= MIN_TIMESTAMP_VALIDITY
            results.append(row)
            del model
        except Exception as exc:
            results.append({"model": model_id, "compute_type": compute_type, "error_type": type(exc).__name__, "error": str(exc)[:800]})

    acceptable = [row for row in results if row.get("acceptable")]
    selected = min(acceptable, key=lambda row: (row.get("realtime_factor") if row.get("realtime_factor") is not None else 9999, row["peak_process_vram_mib"])) if acceptable else None
    if not selected:
        return emit({"summary": "No fixed Whisper configuration met the acceptance thresholds", "thresholds": {"general_accuracy": MIN_GENERAL_ACCURACY, "project_term_recall": MIN_PROJECT_TERM_RECALL, "timestamp_validity": MIN_TIMESTAMP_VALIDITY}, "results": results}, 1)
    return emit({"summary": f"Whisper benchmark selected {selected['model']} / {selected['compute_type']} as the cheapest acceptable measured configuration", "thresholds": {"general_accuracy": MIN_GENERAL_ACCURACY, "project_term_recall": MIN_PROJECT_TERM_RECALL, "timestamp_validity": MIN_TIMESTAMP_VALIDITY}, "selected": selected, "results": results})


if __name__ == "__main__":
    raise SystemExit(main())
