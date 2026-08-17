import json
from pathlib import Path

from app.core.config import ROOT_DIR, get_settings
from app.models import Stream


EDITORIAL_PROMPT = """You are reviewing an archived Shocks Art livestream as an assistant editor.

Your job is not to create the final short and not to develop a content strategy.

Your job is to identify the strongest source-footage windows in this livestream for later short-form editing.

Each candidate is a source window for later editing, not a finished short. The preferred window is approximately five to ten minutes. Do not return a one-minute final-short-sized clip unless the stream truly contains no longer coherent source window for that beat. When you choose a shorter window, explain why in contextual_notes and lower editing_potential if it limits downstream editing.

The content pillars are:
1. motivational and inspirational;
2. personal journey and recovery;
3. artistic process;
4. explanation and education;
5. humor and personality;
6. mistakes and problem-solving;
7. emergent or miscellaneous.

For each candidate, select exactly one primary pillar. Favor footage with a compelling opening, visible action, Nate or his hands in frame, clear views of the work, understandable speech, a distinct topic or emotional arc, honest standalone context, and enough substance to produce one or more shorts.

Motivational or personal audio may still be valuable when the visuals are useful as process footage or when the audio could later become voiceover.

Penalize dead air, repetitive greetings, administrative discussion, poor visuals, unclear audio, excessive context requirements, repetition, and moments that could become misleading when clipped.

Apply editorial judgment inside these rules. Return the strongest candidates even when they are imperfect.

Return up to {candidate_count} ranked candidates in a top-level "candidates" array, strongest first. Prefer {candidate_count} candidates when the stream contains enough distinct usable material.

Candidates should not substantially overlap. Prefer a diverse set of useful source windows over three near-duplicates from the same moment.

Explain why each candidate is strong for this stream. Do not claim that any candidate is strongest across the full archive.

Do not choose the first acceptable segment by default. Before selecting, scan the full stream for multiple plausible candidate windows across the beginning, middle, and end when the video length allows it. Prefer the best complete editorial beat, not the earliest usable beat.

In each selection_reason, briefly state what makes that window distinct from the other selected candidates or plausible alternatives. If you cannot confidently compare across the full stream, say so in contextual_notes and lower confidence.

Provide separate component scores rather than relying on one overall score. Use only evidence visible or audible in the supplied video. Do not invent quotations, actions, people, or context.

Transcript and visual evidence must come from inside the selected timestamp range. Add transcript_evidence and visual_evidence arrays with exact timestamps and seconds for the evidence you are relying on. If you cannot verify a spoken line inside the selected range, leave transcript_evidence empty and write "No verified in-window transcript evidence" in transcript_excerpt.

The transcript_excerpt field must summarize or quote only speech from inside the selected range. The visual_description field must describe only visuals from inside the selected range.

Return only valid JSON matching the supplied schema. Use the "candidates" array, not the legacy singular "candidate" field.
"""


REPAIR_PROMPT = """The previous response failed schema validation.

Correct only the formatting, missing fields, field types, enum values, or internal timestamp inconsistencies identified below.

Do not perform a new video analysis.

Do not change the editorial selection unless a listed validation error makes the existing selection impossible to represent.

Return only corrected JSON.

Schema version:
{schema_version}

Validation errors:
{validation_errors}

Previous response:
{previous_response}
"""


class GeminiAnalyzer:
    def __init__(self, api_key: str, model: str, schema_version: str = "1.0") -> None:
        self.api_key = api_key
        self.model = model
        self.schema_version = schema_version

    def build_analysis_prompt(self, stream: Stream) -> str:
        settings = get_settings()
        schema = json.loads((ROOT_DIR / "schemas" / "candidate_window.schema.v1.json").read_text(encoding="utf-8"))
        return (
            f"{EDITORIAL_PROMPT.format(candidate_count=settings.candidates_per_stream)}\n\n"
            f"Schema version: {self.schema_version}\n"
            f"Stream ID: {stream.stream_id}\n"
            f"Source video ID: {stream.source_video_id}\n"
            f"Title: {stream.title}\n"
            f"URL: {stream.url}\n"
            f"Schema:\n{json.dumps(schema)}"
        )

    def analyze_stream(self, stream: Stream) -> str:
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is required for video analysis")

        from google import genai

        client = genai.Client(api_key=self.api_key)
        prompt = self.build_analysis_prompt(stream)
        if hasattr(client, "interactions"):
            response = client.interactions.create(
                model=self.model,
                input=[
                    {"type": "video", "uri": stream.url},
                    {"type": "text", "text": prompt},
                ],
            )
            return getattr(response, "output_text", None) or getattr(response, "text", None) or ""

        from google.genai import types

        response = client.models.generate_content(
            model=self.model,
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part(text=prompt),
                        types.Part(file_data=types.FileData(file_uri=stream.url, mime_type="video/mp4")),
                    ],
                )
            ],
        )
        return response.text or ""

    def build_repair_prompt(self, previous_response: str, validation_errors: list[str]) -> str:
        return REPAIR_PROMPT.format(
            schema_version=self.schema_version,
            validation_errors="\n".join(validation_errors),
            previous_response=previous_response,
        )

    def repair_response(self, previous_response: str, validation_errors: list[str]) -> str:
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is required for response repair")

        from google import genai

        client = genai.Client(api_key=self.api_key)
        response = client.models.generate_content(
            model=self.model,
            contents=self.build_repair_prompt(previous_response, validation_errors),
        )
        return response.text or ""


def raw_response_path(run_id: str) -> Path:
    path = ROOT_DIR / "data" / "raw_responses"
    path.mkdir(parents=True, exist_ok=True)
    return path / f"{run_id}.json"


def debug_log_path(run_id: str) -> Path:
    path = ROOT_DIR / "data" / "debug_logs"
    path.mkdir(parents=True, exist_ok=True)
    return path / f"{run_id}.log"
