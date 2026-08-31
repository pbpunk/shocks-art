import json
from pathlib import Path
from types import SimpleNamespace

from tools.host_profiles import indexer_soak


def test_parent_soak_reads_live_sqlite_only_through_helper() -> None:
    source = (Path(__file__).resolve().parents[1] / "tools" / "host_profiles" / "indexer_soak.py").read_text(encoding="utf-8")

    assert "indexer_soak_live_metrics.py" in source
    assert "from app.core.database" not in source
    assert "from app.library_models" not in source
    assert "collect_long_form_redundancy" not in source


def test_live_metrics_helper_reorders_imports_before_live_app_access() -> None:
    source = (Path(__file__).resolve().parents[1] / "tools" / "host_profiles" / "indexer_soak_live_metrics.py").read_text(encoding="utf-8")

    assert "configure_live_imports()" in source
    assert source.index("configure_live_imports()") < source.index("traces = trace_inventory()")
    assert "from app.core.database" in source
    assert "SHOCKS_INDEXER_SOAK_MODEL_ID" in source
    assert "SHOCKS_INDEXER_SOAK_DIMENSION" in source


def test_read_live_metrics_accepts_only_helper_json(monkeypatch) -> None:
    payload = {
        "ok": True,
        "trace_volume": {"total": 12, "by_type": {"visual": 12}, "source": "live-production-sqlite"},
        "long_form_visual_redundancy": {"available": True, "adjacent_pair_count": 11},
    }
    completed = SimpleNamespace(returncode=0, stdout=json.dumps(payload) + "\n", stderr="ignored secret-bearing stderr")
    monkeypatch.setattr(indexer_soak.subprocess, "run", lambda *args, **kwargs: completed)

    assert indexer_soak.read_live_metrics("model", 2048) == payload


def test_read_live_metrics_fails_closed_without_active_generation() -> None:
    assert indexer_soak.read_live_metrics("", 0) == {
        "ok": False,
        "error_type": "ActiveVisualGenerationUnavailable",
    }
