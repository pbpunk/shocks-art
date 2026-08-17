from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


Pillar = Literal[
    "motivational_inspirational",
    "personal_journey_recovery",
    "artistic_process",
    "explanation_education",
    "humor_personality",
    "mistakes_problem_solving",
    "emergent_miscellaneous",
]


class CandidateScores(BaseModel):
    pillar_relevance: int = Field(ge=0, le=100)
    hook_strength: int = Field(ge=0, le=100)
    standalone_clarity: int = Field(ge=0, le=100)
    visual_quality: int = Field(ge=0, le=100)
    audio_clarity: int = Field(ge=0, le=100)
    emotional_impact: int = Field(ge=0, le=100)
    educational_value: int = Field(ge=0, le=100)
    entertainment_value: int = Field(ge=0, le=100)
    editing_potential: int = Field(ge=0, le=100)
    brand_fit: int = Field(ge=0, le=100)
    confidence: int = Field(ge=0, le=100)


class TimedEvidence(BaseModel):
    timestamp: str
    seconds: int = Field(ge=0)
    text: str


class CandidatePayload(BaseModel):
    title: str
    start_seconds: int = Field(ge=0)
    end_seconds: int = Field(gt=0)
    start_timestamp: str
    end_timestamp: str
    duration_seconds: int = Field(gt=0)
    concise_summary: str
    selection_reason: str
    primary_pillar: Pillar
    secondary_pillars: list[Pillar] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    transcript_excerpt: str = ""
    visual_description: str = ""
    transcript_evidence: list[TimedEvidence] = Field(default_factory=list)
    visual_evidence: list[TimedEvidence] = Field(default_factory=list)
    contextual_notes: str = ""
    estimated_short_count: int = Field(ge=1)
    possible_hooks: list[str] = Field(default_factory=list)
    editing_notes: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    scores: CandidateScores
    emergent_observations: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_time_consistency(self) -> "CandidatePayload":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds")
        if self.duration_seconds != self.end_seconds - self.start_seconds:
            raise ValueError("duration_seconds must equal end_seconds - start_seconds")
        if self.start_timestamp != seconds_to_timestamp(self.start_seconds):
            raise ValueError("start_timestamp must match start_seconds")
        if self.end_timestamp != seconds_to_timestamp(self.end_seconds):
            raise ValueError("end_timestamp must match end_seconds")
        for evidence in [*self.transcript_evidence, *self.visual_evidence]:
            if evidence.timestamp != seconds_to_timestamp(evidence.seconds):
                raise ValueError("evidence timestamp must match evidence seconds")
            if evidence.seconds < self.start_seconds or evidence.seconds > self.end_seconds:
                raise ValueError("evidence seconds must fall inside candidate window")
        return self


class CandidateResponse(BaseModel):
    schema_version: Literal["1.0"]
    stream_id: str
    source_video_id: str
    candidate: CandidatePayload | None = None
    candidates: list[CandidatePayload] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_candidate_presence(self) -> "CandidateResponse":
        if not self.candidates and not self.candidate:
            raise ValueError("response must include candidate or candidates")
        if not self.candidates and self.candidate:
            self.candidates = [self.candidate]
        return self


def seconds_to_timestamp(total_seconds: int) -> str:
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours:02}:{minutes:02}:{seconds:02}"
