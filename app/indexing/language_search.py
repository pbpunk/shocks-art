from __future__ import annotations

import math
import re
import time
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.library_models import Trace


_TOKEN_RE = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?", re.IGNORECASE)


@dataclass(frozen=True)
class LanguageSearchMatch:
    trace_id: str
    media_id: str
    start_ms: int
    end_ms: int
    text: str
    score: float

    def as_dict(self) -> dict:
        return {
            "traceId": self.trace_id,
            "mediaId": self.media_id,
            "startMs": self.start_ms,
            "endMs": self.end_ms,
            "text": self.text,
            "score": round(self.score, 8),
        }


@dataclass(frozen=True)
class LanguageSearchResult:
    trace_count: int
    query_tokens: tuple[str, ...]
    top_k: int
    database_ms: float
    scoring_ms: float
    elapsed_ms: float
    matches: tuple[LanguageSearchMatch, ...]

    def as_dict(self) -> dict:
        return {
            "traceCount": self.trace_count,
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


def tokenize_language_query(value: str) -> tuple[str, ...]:
    return tuple(token.casefold() for token in _TOKEN_RE.findall(str(value or "")))


def search_language_traces(
    db: Session,
    *,
    query: str,
    top_k: int = 10,
    k1: float = 1.5,
    b: float = 0.75,
) -> LanguageSearchResult:
    """Rank caption Language Traces with deterministic BM25-style lexical scoring.

    Only Trace.content_text participates in scoring. Media titles, filenames, paths,
    source URLs, and other presentation metadata are deliberately absent from this
    retrieval function so they cannot leak into semantic relevance.
    """

    if top_k <= 0:
        raise ValueError("top_k must be greater than zero")
    query_tokens = tokenize_language_query(query)
    if not query_tokens:
        raise ValueError("query must contain at least one searchable token")

    started = time.perf_counter()
    database_started = time.perf_counter()
    rows = list(
        db.scalars(
            select(Trace)
            .where(Trace.trace_type == "language", Trace.content_text != "")
            .order_by(Trace.trace_id.asc())
        ).all()
    )
    database_ms = (time.perf_counter() - database_started) * 1000.0

    scoring_started = time.perf_counter()
    documents: list[tuple[Trace, tuple[str, ...], Counter[str]]] = []
    document_frequency: Counter[str] = Counter()
    total_length = 0
    query_terms = set(query_tokens)
    for trace in rows:
        tokens = tokenize_language_query(trace.content_text)
        if not tokens:
            continue
        counts = Counter(tokens)
        documents.append((trace, tokens, counts))
        total_length += len(tokens)
        for term in query_terms.intersection(counts):
            document_frequency[term] += 1

    document_count = len(documents)
    average_length = (total_length / document_count) if document_count else 0.0
    matches: list[LanguageSearchMatch] = []
    if document_count and average_length:
        for trace, tokens, counts in documents:
            score = 0.0
            document_length = len(tokens)
            for term in query_tokens:
                frequency = counts.get(term, 0)
                if not frequency:
                    continue
                frequency_docs = document_frequency.get(term, 0)
                inverse_document_frequency = math.log(
                    1.0 + ((document_count - frequency_docs + 0.5) / (frequency_docs + 0.5))
                )
                denominator = frequency + k1 * (1.0 - b + b * (document_length / average_length))
                score += inverse_document_frequency * ((frequency * (k1 + 1.0)) / denominator)
            if score <= 0.0:
                continue
            matches.append(
                LanguageSearchMatch(
                    trace_id=trace.trace_id,
                    media_id=trace.media_id,
                    start_ms=trace.start_ms,
                    end_ms=trace.end_ms,
                    text=trace.content_text,
                    score=score,
                )
            )

    matches.sort(key=lambda match: (-match.score, match.trace_id))
    scoring_ms = (time.perf_counter() - scoring_started) * 1000.0
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return LanguageSearchResult(
        trace_count=len(rows),
        query_tokens=query_tokens,
        top_k=top_k,
        database_ms=database_ms,
        scoring_ms=scoring_ms,
        elapsed_ms=elapsed_ms,
        matches=tuple(matches[:top_k]),
    )
