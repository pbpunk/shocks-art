from app.models import CandidateWindow


DEFAULT_WEIGHTS = {
    "pillar_relevance": 0.10,
    "hook_strength": 0.14,
    "standalone_clarity": 0.12,
    "visual_quality": 0.12,
    "audio_clarity": 0.08,
    "emotional_impact": 0.10,
    "educational_value": 0.08,
    "entertainment_value": 0.08,
    "editing_potential": 0.14,
    "brand_fit": 0.04,
}


def weighted_score(scores: dict, weights: dict[str, float] | None = None) -> float:
    active_weights = weights or DEFAULT_WEIGHTS
    total_weight = sum(active_weights.values())
    if total_weight <= 0:
        return 0.0
    return round(sum(float(scores.get(key, 0)) * weight for key, weight in active_weights.items()) / total_weight, 2)


def rank_candidates(candidates: list[CandidateWindow], limit: int | None = None) -> list[CandidateWindow]:
    ranked = sorted(candidates, key=lambda c: c.weighted_score, reverse=True)
    return ranked[:limit] if limit else ranked

