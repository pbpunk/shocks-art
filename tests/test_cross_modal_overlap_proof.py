from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "tools" / "host_profiles" / "cross_modal_overlap_proof.py"


def test_cross_modal_proof_binds_relative_runtime_paths_to_live_root_before_app_imports() -> None:
    source = PROFILE.read_text(encoding="utf-8")
    chdir_at = source.index("os.chdir(LIVE_ROOT)")
    database_import_at = source.index("from app.core.database import SessionLocal")
    assert chdir_at < database_import_at


def test_cross_modal_proof_is_fixed_to_one_media_and_two_targeted_jobs() -> None:
    source = PROFILE.read_text(encoding="utf-8")
    assert 'TARGET_MEDIA_ID = "media_66612c0710ad4b8ba78e3653256af2fe"' in source
    assert 'QUERY = "sanding axes"' in source
    assert 'enqueue_job("visual-media", media_id=TARGET_MEDIA_ID)' in source
    assert 'enqueue_job("visual-embeddings", media_id=TARGET_MEDIA_ID)' in source
    assert '"visual-pending"' not in source
    assert "include_remote" not in source


def test_cross_modal_proof_fails_closed_on_worker_contention_and_checks_cleanup() -> None:
    source = PROFILE.read_text(encoding="utf-8")
    assert "if not worker_present" in source
    assert "if busy_jobs" in source
    assert "scratch_initial = scratch_bytes()" in source
    assert "scratch_final = scratch_bytes()" in source
    assert "scratch_clean = scratch_final <= scratch_initial" in source


def test_cross_modal_proof_requires_grounded_language_visual_and_fused_evidence() -> None:
    source = PROFILE.read_text(encoding="utf-8")
    assert 'media.source_type != "youtube"' in source
    assert 'before["languageTraces"] <= 0' in source
    assert 'after["exactGenerationVisualEmbeddings"] > 0' in source
    assert 'query_proof["targetLanguageMatches"] > 0' in source
    assert 'query_proof["targetVisualMatches"] > 0' in source
    assert 'query_proof["targetFusedMatches"] > 0' in source
    assert '"metadataUsedForScoring": False' in source
