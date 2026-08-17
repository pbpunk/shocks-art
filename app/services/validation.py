import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.core.config import ROOT_DIR
from app.schemas.candidate import CandidateResponse


class CandidateValidationError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("Candidate response failed validation")
        self.errors = errors


def schema_path(schema_version: str = "1.0") -> Path:
    return ROOT_DIR / "schemas" / f"candidate_window.schema.v{schema_version.split('.')[0]}.json"


def load_schema(schema_version: str = "1.0") -> dict[str, Any]:
    return json.loads(schema_path(schema_version).read_text(encoding="utf-8"))


def parse_json_response(raw_response: str) -> dict[str, Any]:
    cleaned = raw_response.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    return json.loads(cleaned)


def validate_candidate_response(raw_response: str | dict[str, Any], schema_version: str = "1.0") -> CandidateResponse:
    data = parse_json_response(raw_response) if isinstance(raw_response, str) else raw_response
    schema = load_schema(schema_version)
    json_errors = [
        f"{'/'.join(str(part) for part in error.path) or '<root>'}: {error.message}"
        for error in Draft202012Validator(schema).iter_errors(data)
    ]
    if json_errors:
        raise CandidateValidationError(sorted(json_errors))
    try:
        return CandidateResponse.model_validate(data)
    except ValidationError as exc:
        raise CandidateValidationError([f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in exc.errors()]) from exc

