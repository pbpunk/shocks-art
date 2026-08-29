from pathlib import Path

from tools.host_profiles.indexer_soak_redundancy import (
    normalized_cosine,
    summarize_media_pairs,
)


def test_normalized_cosine_clamps_float_noise() -> None:
    assert normalized_cosine((1.0, 0.0), (1.0, 0.0)) == 1.0
    assert normalized_cosine((1.000001,), (1.000001,)) == 1.0


def test_long_form_redundancy_summary_is_descriptive_and_timestamp_based() -> None:
    summary = summarize_media_pairs(
        media_id="media_long",
        duration_seconds=6000.0,
        timestamps_ms=[0, 60000, 120000, 180000],
        cosines=[0.97, 0.99, 0.999],
    )

    assert summary == {
        "media_id": "media_long",
        "duration_seconds": 6000.0,
        "trace_count": 4,
        "adjacent_pair_count": 3,
        "median_interval_seconds": 60.0,
        "mean_cosine": 0.986333,
        "p50_cosine": 0.99,
        "p95_cosine": 0.999,
        "fraction_ge_0_98": 0.666667,
        "fraction_ge_0_995": 0.333333,
    }


def test_long_form_redundancy_query_never_loads_semantic_metadata() -> None:
    source = (Path(__file__).resolve().parents[1] / "tools" / "host_profiles" / "indexer_soak_redundancy.py").read_text(encoding="utf-8")
    query_section = source.split("db.execute(", 1)[1]

    assert "Media.filename" not in query_section
    assert "Media.title" not in query_section
    assert "Media.source_path" not in query_section
    assert 'Trace.trace_type == "visual"' in query_section
    assert "Embedding.normalized.is_(True)" in query_section
