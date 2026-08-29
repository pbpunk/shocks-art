from app.indexing.language_search import LanguageSearchMatch
from app.indexing.retrieval_fusion import fuse_temporal_retrieval, temporal_gap_ms
from app.indexing.visual_search import VisualSearchMatch


def language_match(
    trace_id: str,
    media_id: str,
    start_ms: int,
    end_ms: int,
    *,
    score: float = 1.0,
    text: str = "spoken evidence",
) -> LanguageSearchMatch:
    return LanguageSearchMatch(
        trace_id=trace_id,
        trace_ids=(trace_id,),
        media_id=media_id,
        start_ms=start_ms,
        end_ms=end_ms,
        text=text,
        score=score,
        matched_terms=("evidence",),
    )


def visual_match(
    trace_id: str,
    media_id: str,
    start_ms: int,
    end_ms: int,
    *,
    score: float = 0.5,
) -> VisualSearchMatch:
    return VisualSearchMatch(
        trace_id=trace_id,
        media_id=media_id,
        start_ms=start_ms,
        end_ms=end_ms,
        artifact_path=f"visual/{trace_id}.jpg",
        score=score,
    )


def test_temporal_gap_handles_overlap_and_separation():
    assert temporal_gap_ms(1000, 2000, 1500, 2500) == 0
    assert temporal_gap_ms(1000, 2000, 2500, 3000) == 500
    assert temporal_gap_ms(2500, 3000, 1000, 2000) == 500


def test_fusion_requires_same_media_and_bounded_temporal_distance():
    language = [
        language_match("language-near", "media-a", 10_000, 20_000),
        language_match("language-other", "media-b", 10_000, 20_000),
        language_match("language-far", "media-a", 300_000, 310_000),
    ]
    visual = [visual_match("visual-a", "media-a", 25_000, 25_000)]

    fused = fuse_temporal_retrieval(language, visual, top_k=5, max_gap_ms=60_000)

    assert len(fused) == 1
    assert fused[0].media_id == "media-a"
    assert fused[0].language_trace_id == "language-near"
    assert fused[0].visual_trace_id == "visual-a"
    assert fused[0].gap_ms == 5_000


def test_fusion_uses_rank_and_proximity_without_mixing_raw_score_scales():
    language = [
        language_match(
            "language-rank-1",
            "media-a",
            0,
            10_000,
            score=0.01,
            text="ranked language evidence",
        ),
        language_match("language-rank-2", "media-b", 0, 10_000, score=9_999.0),
    ]
    visual = [
        visual_match("visual-rank-1", "media-a", 10_000, 10_000, score=0.01),
        visual_match("visual-rank-2", "media-b", 100_000, 100_000, score=9_999.0),
    ]

    fused = fuse_temporal_retrieval(language, visual, top_k=5, max_gap_ms=120_000)

    assert [match.media_id for match in fused] == ["media-a", "media-b"]
    assert fused[0].gap_ms == 0
    assert fused[0].language_score == 0.01
    assert fused[0].visual_score == 0.01
    assert fused[1].language_score == 9_999.0
    assert fused[1].visual_score == 9_999.0


def test_fusion_keeps_one_best_language_neighborhood_per_visual_trace():
    language = [
        language_match("language-rank-1", "media-a", 0, 5_000),
        language_match("language-nearer", "media-a", 19_000, 21_000),
    ]
    visual = [visual_match("visual-a", "media-a", 20_000, 20_000)]

    fused = fuse_temporal_retrieval(
        language,
        visual,
        top_k=5,
        max_gap_ms=120_000,
        rrf_k=0,
        proximity_weight=1.0,
    )

    assert len(fused) == 1
    # Rank remains primary enough that a high-ranked language hit is not silently
    # replaced merely because another hit overlaps the frame more closely.
    assert fused[0].language_trace_id == "language-rank-1"
    payload = fused[0].as_dict()
    assert payload["language"]["text"] == "spoken evidence"
    assert payload["visual"]["artifactPath"] == "visual/visual-a.jpg"
