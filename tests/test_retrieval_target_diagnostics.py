from __future__ import annotations

from pathlib import Path

from app.indexing.language_search import LanguageSearchMatch
from app.indexing.retrieval_diagnostics import depth_flags, nearest_language, nearest_visual
from app.indexing.visual_search import VisualSearchMatch


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "tools" / "host_profiles" / "retrieval_target_diagnostics.py"


def _language(start_ms: int, end_ms: int, *, media_id: str = "target") -> LanguageSearchMatch:
    return LanguageSearchMatch(
        trace_id=f"language-{start_ms}",
        trace_ids=(f"language-{start_ms}",),
        media_id=media_id,
        start_ms=start_ms,
        end_ms=end_ms,
        text="fixed query evidence",
        score=1.0,
        matched_terms=("fixed",),
    )


def _visual(start_ms: int, *, media_id: str = "target") -> VisualSearchMatch:
    return VisualSearchMatch(
        trace_id=f"visual-{start_ms}",
        media_id=media_id,
        start_ms=start_ms,
        end_ms=start_ms,
        artifact_path=f"visual/{start_ms}.jpg",
        score=0.5,
    )


def test_fixed_targets_cover_two_unresolved_queries_and_sign_control() -> None:
    source = PROFILE.read_text(encoding="utf-8")
    for value in (
        '"queryId": "fractal-burning-setup"',
        '"query": "fractal burning setup"',
        '"mediaId": "media_4a2b9b61b1cd44e7bd820ed68dbf207d"',
        '"queryId": "finished-staffs"',
        '"query": "finished staffs"',
        '"mediaId": "media_0a571dc5e48942fc9b9d98e27609eeb0"',
        '"queryId": "gluing-sign-control"',
        '"query": "gluing letters onto a sign"',
        '"mediaId": "media_53c498d982c14ec680bacf2be2f4dfa0"',
    ):
        assert value in source


def test_nearest_visual_prefers_temporal_grounding_before_global_rank() -> None:
    language = _language(1000, 2000)
    ranked_visual = [(1, _visual(9000)), (47, _visual(1500))]
    nearest = nearest_visual(language, ranked_visual)
    assert nearest is not None
    rank, visual, gap_ms = nearest
    assert rank == 47
    assert visual.start_ms == 1500
    assert gap_ms == 0


def test_nearest_language_prefers_temporal_grounding_before_global_rank() -> None:
    visual = _visual(5000)
    ranked_language = [(2, _language(20000, 21000)), (91, _language(4500, 5500))]
    nearest = nearest_language(visual, ranked_language)
    assert nearest is not None
    rank, language, gap_ms = nearest
    assert rank == 91
    assert language.start_ms == 4500
    assert gap_ms == 0


def test_candidate_depth_flags_make_rank_cutoffs_explicit() -> None:
    assert depth_flags(25) == {"withinTop25": True, "withinTop50": True, "withinTop100": True}
    assert depth_flags(26) == {"withinTop25": False, "withinTop50": True, "withinTop100": True}
    assert depth_flags(101) == {"withinTop25": False, "withinTop50": False, "withinTop100": False}


def test_profile_is_read_only_and_binds_live_root_before_database_import() -> None:
    source = PROFILE.read_text(encoding="utf-8")
    assert "enqueue_job" not in source
    assert "visual-pending" not in source
    assert '"stateMutationRequested": False' in source
    assert '"metadataUsedForSelectionOrScoring": False' in source
    assert "os.chdir(LIVE_ROOT)" in source
    assert source.index("os.chdir(LIVE_ROOT)") < source.index("from app.core.database import SessionLocal")
