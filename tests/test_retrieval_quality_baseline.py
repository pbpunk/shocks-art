from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "tools" / "retrieval_quality_live.py"
LIBRARY_ROUTES = ROOT / "app" / "library_routes.py"


def test_retrieval_quality_uses_measured_candidate_depth() -> None:
    source = PROFILE.read_text(encoding="utf-8")
    assert "CANDIDATE_K = 100" in source
    assert "top_k=CANDIDATE_K" in source
    assert '"candidatePoolK": CANDIDATE_K' in source


def test_retrieval_quality_reports_unchanged_temporal_policy() -> None:
    source = PROFILE.read_text(encoding="utf-8")
    assert "DEFAULT_MAX_GAP_MS" in source
    assert '"temporalMaxGapMs": DEFAULT_MAX_GAP_MS' in source
    assert "max_gap_ms=" not in source


def test_retrieval_quality_receipt_is_compact_bounded_and_versioned() -> None:
    source = PROFILE.read_text(encoding="utf-8")
    assert '"schemaVersion": 6' in source
    assert "MAX_RECEIPT_JSON_CHARS = 28_000" in source
    assert "TEXT_SNIPPET_CHARS = 160" in source
    assert '"receiptBudgetChars": MAX_RECEIPT_JSON_CHARS' in source
    assert "match.as_dict()" not in source
    assert "language_trace_ids" not in source
    assert "visual_artifact_path" not in source
    assert "receipt exceeded bridge budget" in source


def test_retrieval_quality_remains_evaluation_only_and_metadata_isolated() -> None:
    source = PROFILE.read_text(encoding="utf-8")
    assert '"filenameUsed": False' in source
    assert '"titleUsed": False' in source
    assert '"sourcePathUsed": False' in source
    assert "enqueue_job" not in source
    assert "library_routes" not in source


def test_production_library_route_remains_visual_only() -> None:
    source = LIBRARY_ROUTES.read_text(encoding="utf-8")
    assert '@router.post("/api/library/search/visual")' in source
    assert "search_visual_embeddings(" in source
    assert "fuse_temporal_retrieval" not in source
