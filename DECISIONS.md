# Decisions

## 2026-07-31: Local-first FastAPI MVP

Chosen stack: FastAPI, SQLite, SQLAlchemy, Pydantic, JSON Schema, YouTube Data API, Gemini SDK, server-rendered Jinja UI.

Reason: it delivers the complete vertical slice with minimal infrastructure and clear future migration paths.

## 2026-07-31: Gemini is an adapter

Gemini analysis is isolated in `app/services/gemini.py`. Persistence, ranking, review, retry, and export remain application responsibilities.

## 2026-07-31: Store raw responses

Raw Gemini responses are written under `data/raw_responses/` and linked from `AnalysisRun` for audit and repair.

## 2026-07-31: Treat extraction as a visible job

Candidate extraction can take time and can fail because of quota, credentials, video availability, or validation. The product should show active work, recent attempts, model used, and actionable error text in the UI instead of requiring a terminal.

## 2026-07-31: Return several candidates per stream

Single-candidate extraction over-compresses editorial judgment and makes it hard to calibrate scoring. New runs ask Gemini for up to three ranked, non-overlapping windows per stream while preserving per-window review status.

## 2026-07-31: Native YouTube Ask is the trusted extraction lane

The Gemini API did not reliably show the same video-grounded behavior as YouTube's Ask sidebar and produced at least one unverifiable quote. Native YouTube Ask responses are now imported through a separate lane with raw response storage, source provenance, parser tests, and a browser automation runner. API-generated candidates remain reviewable, but native Ask candidates are visually distinguishable by `AnalysisRun.model`.
