import csv
import io
import json

from app.models import CandidateWindow


def candidate_to_dict(candidate: CandidateWindow) -> dict:
    stream = candidate.stream
    return {
        "candidate_window_id": candidate.candidate_window_id,
        "stream_id": candidate.stream_id,
        "source_video_id": stream.source_video_id,
        "stream_title": stream.title,
        "source_url": stream.url,
        "start_seconds": candidate.start_seconds,
        "end_seconds": candidate.end_seconds,
        "start_timestamp": candidate.start_timestamp,
        "end_timestamp": candidate.end_timestamp,
        "title": candidate.title,
        "primary_pillar": candidate.primary_pillar,
        "tags": candidate.tags,
        "weighted_score": candidate.weighted_score,
        "confidence": candidate.confidence,
        "review_status": candidate.review_status,
        "estimated_short_count": candidate.estimated_short_count,
        "selection_reason": candidate.selection_reason,
        "visual_description": candidate.visual_description,
    }


def export_json(candidates: list[CandidateWindow]) -> str:
    return json.dumps([candidate_to_dict(candidate) for candidate in candidates], indent=2)


def export_csv(candidates: list[CandidateWindow]) -> str:
    output = io.StringIO()
    rows = [candidate_to_dict(candidate) for candidate in candidates]
    fieldnames = list(rows[0].keys()) if rows else ["candidate_window_id"]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        row = {**row, "tags": "|".join(row["tags"])}
        writer.writerow(row)
    return output.getvalue()

