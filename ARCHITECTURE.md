# Architecture

## Decision

Use Python, FastAPI, SQLite, SQLAlchemy, Pydantic, JSON Schema, the YouTube Data API, and the Gemini SDK.

## Rationale

FastAPI keeps the local web app and JSON API in one understandable service. SQLite is durable, easy to back up, and sufficient for an MVP. SQLAlchemy provides a clean migration path to Postgres later. Pydantic plus JSON Schema gives deterministic validation before persistence. The official YouTube Data API avoids rendered-page scraping. Gemini remains a bounded video-analysis adapter, not the system of record.

## Background Jobs

The first slice runs processing synchronously through API endpoints with resumable statuses and retry state. This avoids extra infrastructure. A later worker can reuse `app/services/processing.py`.

## Secret Management

Secrets live in `.env`, which should not be committed. `.env.example` documents required keys.

## Observability

`AnalysisRun` records prompt version, model, schema version, status, retries, validation errors, raw response location, usage, and estimated cost fields.

## Deployment Assumption

Local-first or small single-instance hosting. No distributed queue or object store is required for MVP.

