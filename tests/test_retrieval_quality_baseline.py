from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "tools" / "retrieval_quality_live.py"
LIBRARY_ROUTES = ROOT / "app" / "library_routes.py"


def test_retrieval_quality_uses_measured_candidate_depth() -> None:
    source = PROFILE.read_text(encoding="utf-8")
    assert "CANDIDATE_K = 100" in source
    assert '"candidatePoolK": CANDIDATE_K' in source
    assert '"candidatePoolK": CANDIDATE_K' in source


def test_retrieval_quality_reports_unchanged_temporal_policy() -> None:
    source = PROFILE.read_text(encoding="utf-8")
    assert "DEFAULT_MAX_GAP_MS" in source
    assert '"temporalMaxGapMs": DEFAULT_MAX_GAP_MS' in source
    assert "max_gap_ms=" not in source


def test_retrieval_quality_receipt_schema_is_versioned() -> None:
    source = PROFILE.read_text(encoding="utf-8")
    assert '"schemaVersion": 5' in source


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
