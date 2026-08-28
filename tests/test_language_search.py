from app.indexing.language_search import search_language_traces, tokenize_language_query
from app.library_models import Media, Trace


def _media(db_session, source_id: str, *, title: str = "", filename: str = "") -> Media:
    media = Media(
        source_type="youtube",
        source_id=source_id,
        source_url="",
        source_path="",
        title=title,
        filename=filename,
        checksum_sha256=f"{source_id:0<64}"[:64],
        media_kind="video",
    )
    db_session.add(media)
    db_session.flush()
    return media


def _trace(db_session, media: Media, start_ms: int, text: str, trace_type: str = "language") -> Trace:
    trace = Trace(
        media_id=media.media_id,
        trace_type=trace_type,
        start_ms=start_ms,
        end_ms=start_ms + 3000,
        content_text=text,
        extractor="fixture",
        extractor_version="1",
        configuration_hash=f"{start_ms:064x}"[-64:],
    )
    db_session.add(trace)
    db_session.flush()
    return trace


def test_tokenize_language_query_is_casefolded_and_metadata_agnostic():
    assert tokenize_language_query("Sanding AXES, epoxy's cure") == ("sanding", "axes", "epoxy's", "cure")


def test_language_search_ranks_trace_text_not_media_title_or_filename(db_session):
    relevant_media = _media(db_session, "relevant")
    relevant = _trace(db_session, relevant_media, 120000, "we are sanding these axes before the next finish coat")

    leakage_media = _media(
        db_session,
        "leakage",
        title="SANDING AXES PERFECT MATCH",
        filename="sanding_axes_perfect_match.mp4",
    )
    _trace(db_session, leakage_media, 30000, "today we are mixing blue pigment into clear epoxy")
    _trace(db_session, leakage_media, 60000, "sanding axes", trace_type="visual")
    db_session.commit()

    result = search_language_traces(db_session, query="sanding axes", top_k=5)

    assert result.trace_count == 2
    assert result.matches
    assert result.matches[0].trace_id == relevant.trace_id
    assert all(match.media_id != leakage_media.media_id for match in result.matches)
    assert result.as_dict()["scoringIsolation"] == {
        "usesLanguageTraceTextOnly": True,
        "filenameUsed": False,
        "titleUsed": False,
        "sourcePathUsed": False,
    }


def test_language_search_is_stable_for_equal_scores(db_session):
    media = _media(db_session, "stable")
    first = _trace(db_session, media, 1000, "fractal burning setup")
    second = _trace(db_session, media, 2000, "fractal burning setup")
    db_session.commit()

    result = search_language_traces(db_session, query="fractal burning setup", top_k=5)

    expected = sorted([first.trace_id, second.trace_id])
    assert [match.trace_id for match in result.matches] == expected


def test_language_search_rejects_blank_query(db_session):
    try:
        search_language_traces(db_session, query="   ")
    except ValueError as exc:
        assert "searchable token" in str(exc)
    else:
        raise AssertionError("blank language query should fail")
