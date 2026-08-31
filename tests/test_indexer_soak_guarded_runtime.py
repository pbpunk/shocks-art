from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_indexer_soak_routes_through_guarded_runtime():
    source = (ROOT / "tools" / "run_host_profile.py").read_text(encoding="utf-8")
    assert '"indexer-soak": ROOT / "tools" / "host_profiles" / "indexer_soak_guarded.py"' in source


def test_guarded_runtime_uses_file_backed_lifecycle_capture_and_stage_markers():
    source = (ROOT / "tools" / "host_profiles" / "indexer_soak_guarded.py").read_text(encoding="utf-8")
    assert "INDEXER_SOAK_STAGE" in source
    assert "stdout=stdout_file" in source
    assert "stderr=stderr_file" in source
    assert "capture_output=True" not in source
    assert 'stage("lifecycle-start"' in source
    assert 'stage("semantic-search-start"' in source
    assert 'stage("live-metrics-start"' in source
