from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "tools" / "host_profiles" / "retrieval_depth_sweep.py"


def test_depth_sweep_uses_fixed_bounded_candidate_depths() -> None:
    source = PROFILE.read_text(encoding="utf-8")
    assert "DEPTHS = (25, 50, 100, 500)" in source
    assert "MAX_DEPTH = max(DEPTHS)" in source
    assert "top_k=MAX_DEPTH" in source
    assert "top_k=5000" in source


def test_depth_sweep_keeps_temporal_rule_and_scoring_isolation_fixed() -> None:
    source = PROFILE.read_text(encoding="utf-8")
    assert '"temporalRuleChanged": False' in source
    assert '"metadataUsedForSelectionOrScoring": False' in source
    assert '"stateMutationRequested": False' in source
    assert "enqueue_job" not in source
    assert "visual-pending" not in source


def test_depth_sweep_reports_expected_target_media_without_using_metadata() -> None:
    source = PROFILE.read_text(encoding="utf-8")
    for media_id in (
        "media_66612c0710ad4b8ba78e3653256af2fe",
        "media_4a2b9b61b1cd44e7bd820ed68dbf207d",
        "media_0a571dc5e48942fc9b9d98e27609eeb0",
        "media_53c498d982c14ec680bacf2be2f4dfa0",
    ):
        assert media_id in source
    assert "filename" not in source
    assert "source_path" not in source
    assert "title" not in source


def test_depth_sweep_fixes_semantic_language_anchors_for_decision_receipt() -> None:
    source = PROFILE.read_text(encoding="utf-8")
    for trace_id in (
        "trace_1a81f877bcba4a4aa04d647745424d14",
        "trace_881afad9478944918286f370a9aa1721",
        "trace_15f39d71d81241cbb22519233b4e347e",
    ):
        assert trace_id in source
    assert '"anchorLanguagePresent"' in source
    assert '"anchorFusedCount"' in source
    assert '"anchorInGlobalTop5"' in source
    assert '"anchorGlobal"' in source


def test_depth_sweep_receipt_is_decision_only_and_bridge_bounded() -> None:
    source = PROFILE.read_text(encoding="utf-8")
    assert "MAX_RECEIPT_JSON_CHARS = 28_000" in source
    assert "TEXT_SNIPPET_CHARS = 56" in source
    assert '"globalMediaIds"' in source
    assert '"expectedMediaInGlobalTop5"' in source
    assert '"expectedGlobalBest"' in source
    assert 'depth_payload["globalTop"]' in source
    assert '"globalFused":' not in source
    assert '"targetFused":' not in source
    assert '"targetTop"' not in source
    assert "visualTraceId" in source
    assert "languageTraceId" in source
    assert "_global_rank_map" in source


def test_depth_sweep_binds_live_root_before_database_import() -> None:
    source = PROFILE.read_text(encoding="utf-8")
    assert source.index("os.chdir(LIVE_ROOT)") < source.index("from app.core.database import SessionLocal")
