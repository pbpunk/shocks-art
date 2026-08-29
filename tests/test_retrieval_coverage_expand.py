from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "tools" / "host_profiles" / "retrieval_coverage_expand.py"


def test_retrieval_coverage_expand_is_fixed_to_three_transcript_identified_media() -> None:
    source = PROFILE.read_text(encoding="utf-8")
    assert '"media_4a2b9b61b1cd44e7bd820ed68dbf207d"' in source
    assert '"media_0a571dc5e48942fc9b9d98e27609eeb0"' in source
    assert '"media_53c498d982c14ec680bacf2be2f4dfa0"' in source
    assert '"fractal-burning-setup"' in source
    assert '"finished-staffs"' in source
    assert '"gluing-sign"' in source
    assert '"visual-pending"' not in source
    assert "include_remote" not in source


def test_retrieval_coverage_expand_uses_live_root_before_database_import() -> None:
    source = PROFILE.read_text(encoding="utf-8")
    assert "os.chdir(LIVE_ROOT)" in source
    assert source.index("os.chdir(LIVE_ROOT)") < source.index("from app.core.database import SessionLocal")


def test_retrieval_coverage_expand_is_singleton_scoped_idempotent_and_cleanup_checked() -> None:
    source = PROFILE.read_text(encoding="utf-8")
    assert "if not worker_present" in source
    assert "if busy_jobs" in source
    assert 'enqueue_job("visual-media", media_id=media_id)' in source
    assert 'enqueue_job("visual-embeddings", media_id=media_id)' in source
    assert 'extraction_status = "already-present"' in source
    assert 'embedding_status = "already-present"' in source
    assert "scratch_final <= scratch_initial" in source
    assert '"bulkRemoteIndexingUsed": False' in source
    assert '"metadataUsedForSelectionOrScoring": False' in source
