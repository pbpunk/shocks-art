from __future__ import annotations

import math
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.library_models import Trace


_TOKEN_RE = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?", re.IGNORECASE)
DEFAULT_WINDOW_RADIUS_MS = 15000
DEFAULT_MAX_WINDOW_MS = 35000


@dataclass(frozen=True)
class LanguageSearchMatch:
    trace_id: str
    trace_ids: tuple[str, ...]
    media_id: str
    start_ms: int
    end_ms: int
    text: str
    score: float
    matched_terms: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "traceId": self.trace_id,
            "traceIds": list(self.trace_ids),
            "mediaId": self.media_id,
            "startMs": self.start_ms,
            "endMs": self.end_ms,
            "text": self.text,
            "score": round(self.score, 8),
            "matchedTerms": list(self.matched_terms),
        }


@dataclass(frozen=True)
class LanguageSearchResult:
    trace_count: int
    window_count: int
    query_tokens: tuple[str, ...]
    top_k: int
    database_ms: float
    scoring_ms: float
    elapsed_ms: float
    matches: tuple[LanguageSearchMatch, ...]

    def as_dict(self) -> dict:
        return {
            "traceCount": self.trace_count,
            "windowCount": self.window_count,
            "queryTokens": list(self.query_tokens),
            "topK": self.top_k,
            "returned": len(self.matches),
            "databaseMs": round(self.database_ms, 4),
            "scoringMs": round(self.scoring_ms, 4),
            "elapsedMs": round(self.elapsed_ms, 4),
            "matches": [match.as_dict() for match in self.matches],
            "scoringIsolation": {
                "usesLanguageTraceTextOnly": True,
                "filenameUsed": False,
                "titleUsed": False,
                "sourcePathUsed": False,
            },
        }


@dataclass(frozen=True)
class _Document:
    trace: Trace
    tokens: tuple[str, ...]
    counts: Counter[str]


@dataclass(frozen=True)
class _Window:
    media_id: str
    traces: tuple[_Document, ...]
    start_ms: int
    end_ms: int
    tokens: tuple[str, ...]
    counts: Counter[str]


def tokenize_language_query(value: str) -> tuple[str, ...]:
    return tuple(token.casefold() for token in _TOKEN_RE.findall(str(value or "")))


def _build_candidate_windows(
    documents: list[_Document],
    query_terms: set[str],
    *,
    radius_ms: int,
    max_window_ms: int,
) -> list[_Window]:
    by_media: dict[str, list[_Document]] = defaultdict(list)
    for document in documents:
        by_media[document.trace.media_id].append(document)

    windows: list[_Window] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for media_id, media_documents in by_media.items():
        media_documents.sort(key=lambda item: (item.trace.start_ms, item.trace.trace_id))
        hit_indexes = [
            index
            for index, document in enumerate(media_documents)
            if query_terms.intersection(document.counts)
        ]
        for hit_index in hit_indexes:
            hit = media_documents[hit_index]
            center_ms = (hit.trace.start_ms + hit.trace.end_ms) // 2
            lower = max(0, center_ms - radius_ms)
            upper = center_ms + radius_ms
            selected = [
                document
                for document in media_documents
                if document.trace.end_ms >= lower and document.trace.start_ms <= upper
            ]
            if not selected:
                continue
            start_ms = selected[0].trace.start_ms
            end_ms = selected[-1].trace.end_ms
            if end_ms - start_ms > max_window_ms:
                selected = [
                    document
                    for document in selected
                    if document.trace.start_ms <= hit.trace.start_ms + radius_ms
                    and document.trace.end_ms >= hit.trace.end_ms - radius_ms
                ]
                start_ms = selected[0].trace.start_ms
                end_ms = selected[-1].trace.end_ms
            trace_ids = tuple(document.trace.trace_id for document in selected)
            key = (media_id, trace_ids)
            if key in seen:
                continue
            seen.add(key)
            tokens = tuple(token for document in selected for token in document.tokens)
            windows.append(
                _Window(
                    media_id=media_id,
                    traces=tuple(selected),
                    start_ms=start_ms,
                    end_ms=end_ms,
                    tokens=tokens,
                    counts=Counter(tokens),
                )
            )
    return windows


def _coverage_multiplier(query_tokens: tuple[str, ...], counts: Counter[str]) -> float:
    unique_terms = set(query_tokens)
    covered = len(unique_terms.intersection(counts))
    if not unique_terms or not covered:
        return 0.0
    coverage = covered / len(unique_terms)
    # Multi-concept creator queries should prefer one coherent temporal neighborhood
    # containing the whole request over isolated single-word caption fragments.
    return 0.55 + (1.45 * coverage * coverage)


def _compactness_bonus(window: _Window, query_terms: set[str]) -> float:
    matched_positions: list[int] = []
    for index, document in enumerate(window.traces):
        if query_terms.intersection(document.counts):
            matched_positions.append(index)
    if len(matched_positions) < 2:
        return 1.0
    span = matched_positions[-1] - matched_positions[0]
    return 1.0 + (0.20 / (1.0 + span))


def search_language_traces(
    db: Session,
    *,
    query: str,
    top_k: int = 10,
    k1: float = 1.5,
    b: float = 0.75,
    window_radius_ms: int = DEFAULT_WINDOW_RADIUS_MS,
    max_window_ms: int = DEFAULT_MAX_WINDOW_MS,
) -> LanguageSearchResult:
    """Rank coherent caption neighborhoods with metadata-isolated lexical scoring.

    Only Language Trace content participates in scoring. A query-term-bearing caption
    event seeds a bounded temporal neighborhood on the same Media item; adjacent
    caption text is concatenated so concepts spoken a few seconds apart can rank as
    one useful moment. Coverage and compactness prefer windows that satisfy multiple
    query concepts together. Media titles, filenames, paths, and source metadata are
    deliberately absent from scoring.
    """

    if top_k <= 0:
        raise ValueError("top_k must be greater than zero")
    if window_radius_ms <= 0 or max_window_ms <= 0:
        raise ValueError("language search window bounds must be positive")
    query_tokens = tokenize_language_query(query)
    if not query_tokens:
        raise ValueError("query must contain at least one searchable token")

    started = time.perf_counter()
    database_started = time.perf_counter()
    rows = list(
        db.scalars(
            select(Trace)
            .where(Trace.trace_type == "language", Trace.content_text != "")
            .order_by(Trace.media_id.asc(), Trace.start_ms.asc(), Trace.trace_id.asc())
        ).all()
    )
    database_ms = (time.perf_counter() - database_started) * 1000.0

    scoring_started = time.perf_counter()
    query_terms = set(query_tokens)
    documents: list[_Document] = []
    for trace in rows:
        tokens = tokenize_language_query(trace.content_text)
        if tokens:
            documents.append(_Document(trace=trace, tokens=tokens, counts=Counter(tokens)))

    windows = _build_candidate_windows(
        documents,
        query_terms,
        radius_ms=window_radius_ms,
        max_window_ms=max_window_ms,
    )
    document_frequency: Counter[str] = Counter()
    total_length = 0
    for window in windows:
        total_length += len(window.tokens)
        for term in query_terms.intersection(window.counts):
            document_frequency[term] += 1

    window_count = len(windows)
    average_length = (total_length / window_count) if window_count else 0.0
    matches: list[LanguageSearchMatch] = []
    if window_count and average_length:
        for window in windows:
            score = 0.0
            for term in query_tokens:
                frequency = window.counts.get(term, 0)
                if not frequency:
                    continue
                frequency_docs = document_frequency.get(term, 0)
                inverse_document_frequency = math.log(
                    1.0 + ((window_count - frequency_docs + 0.5) / (frequency_docs + 0.5))
                )
                denominator = frequency + k1 * (
                    1.0 - b + b * (len(window.tokens) / average_length)
                )
                score += inverse_document_frequency * ((frequency * (k1 + 1.0)) / denominator)
            score *= _coverage_multiplier(query_tokens, window.counts)
            score *= _compactness_bonus(window, query_terms)
            if score <= 0.0:
                continue
            trace_ids = tuple(document.trace.trace_id for document in window.traces)
            matches.append(
                LanguageSearchMatch(
                    trace_id=trace_ids[0],
                    trace_ids=trace_ids,
                    media_id=window.media_id,
                    start_ms=window.start_ms,
                    end_ms=window.end_ms,
                    text=" ".join(document.trace.content_text for document in window.traces),
                    score=score,
                    matched_terms=tuple(sorted(query_terms.intersection(window.counts))),
                )
            )

    matches.sort(key=lambda match: (-match.score, match.media_id, match.start_ms, match.trace_id))
    # Overlapping seeded windows from the same moment are near-duplicates. Keep only
    # the strongest local window so top-K represents distinct editing neighborhoods.
    distinct: list[LanguageSearchMatch] = []
    for match in matches:
        if any(
            existing.media_id == match.media_id
            and match.start_ms <= existing.end_ms
            and existing.start_ms <= match.end_ms
            for existing in distinct
        ):
            continue
        distinct.append(match)
        if len(distinct) >= top_k:
            break

    scoring_ms = (time.perf_counter() - scoring_started) * 1000.0
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return LanguageSearchResult(
        trace_count=len(rows),
        window_count=window_count,
        query_tokens=query_tokens,
        top_k=top_k,
        database_ms=database_ms,
        scoring_ms=scoring_ms,
        elapsed_ms=elapsed_ms,
        matches=tuple(distinct),
    )
