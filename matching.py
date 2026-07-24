from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from typing import Any, Iterable


@dataclass(frozen=True)
class SessionMatch:
    session_key: str
    activity_id: int
    score: float
    method: str
    day_difference: int
    duration_ratio: float
    name_similarity: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class MatchResult:
    matches: tuple[SessionMatch, ...]
    unmatched_session_keys: tuple[str, ...]
    unmatched_activity_ids: tuple[int, ...]


_STOP_WORDS = {
    "ai",
    "base",
    "v3",
    "v4",
    "v5",
    "week",
    "wk",
    "min",
    "minutes",
    "session",
    "workout",
    "training",
}


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _identifier(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _normalize_name(value: Any) -> str:
    text = str(value or "").lower()
    text = re.sub(r"\b\d+(?:\.\d+)?\s*(?:min|minutes|km|m|h)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    tokens = [token for token in text.split() if token not in _STOP_WORDS and not token.isdigit()]
    return " ".join(tokens)


def _name_similarity(planned: dict[str, Any], activity: Any) -> float:
    left = _normalize_name(planned.get("name"))
    right = _normalize_name(getattr(activity, "name", None))
    if not left or not right:
        return 0.0
    sequence = SequenceMatcher(None, left, right).ratio()
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    overlap = len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))
    return max(sequence, overlap)


def _duration_ratio(planned_minutes: float, completed_minutes: float) -> float:
    if planned_minutes <= 0 or completed_minutes <= 0:
        return 0.0
    return min(planned_minutes, completed_minutes) / max(planned_minutes, completed_minutes)


def _candidate(
    session: dict[str, Any],
    activity: Any,
    *,
    day_tolerance: int,
) -> SessionMatch | None:
    session_key = str(session.get("key") or session.get("session_key") or "")
    activity_id = _identifier(getattr(activity, "activity_id", None))
    if not session_key or activity_id is None:
        return None
    if str(session.get("sport") or "") != str(getattr(activity, "sport", "")):
        return None
    try:
        planned_day = date.fromisoformat(str(session.get("day")))
        activity_day = date.fromisoformat(str(getattr(activity, "day")))
    except (TypeError, ValueError):
        return None
    day_difference = abs((activity_day - planned_day).days)
    if day_difference > max(0, day_tolerance):
        return None

    planned_minutes = max(1.0, _number(session.get("duration_minutes")) or 1.0)
    completed_minutes = max(1.0, _number(getattr(activity, "duration_minutes", None)) or 1.0)
    duration_ratio = _duration_ratio(planned_minutes, completed_minutes)
    name_similarity = _name_similarity(session, activity)

    planned_workout_id = _identifier(
        session.get("workout_id")
        or session.get("_record_workout_id")
        or session.get("garmin_workout_id")
    )
    activity_workout_id = _identifier(getattr(activity, "workout_id", None))
    exact_workout = (
        planned_workout_id is not None
        and activity_workout_id is not None
        and planned_workout_id == activity_workout_id
    )

    score = 0.0
    reasons: list[str] = []
    if day_difference == 0:
        score += 42.0
        reasons.append("same calendar day")
    else:
        score += 18.0
        reasons.append(f"activity is {day_difference} day from the planned date")

    if exact_workout:
        score += 40.0
        reasons.append("Garmin workout ID matches")

    score += 23.0 * duration_ratio
    reasons.append(f"duration similarity {duration_ratio:.0%}")

    score += 20.0 * name_similarity
    if name_similarity >= 0.35:
        reasons.append(f"name similarity {name_similarity:.0%}")

    role = str(session.get("role") or "").lower()
    normalized_activity_name = _normalize_name(getattr(activity, "name", None))
    if role and role in normalized_activity_name:
        score += 5.0
        reasons.append("activity name contains the planned role")

    score = min(100.0, round(score, 2))
    if exact_workout:
        method = "garmin_workout_id"
    elif name_similarity >= 0.45:
        method = "date_sport_name_duration"
    else:
        method = "date_sport_duration"

    return SessionMatch(
        session_key=session_key,
        activity_id=activity_id,
        score=score,
        method=method,
        day_difference=day_difference,
        duration_ratio=round(duration_ratio, 4),
        name_similarity=round(name_similarity, 4),
        reasons=tuple(reasons),
    )


def match_planned_sessions(
    planned: Iterable[dict[str, Any]],
    activities: Iterable[Any],
    *,
    day_tolerance: int = 1,
    minimum_score: float = 55.0,
) -> MatchResult:
    planned_items = [item for item in planned if isinstance(item, dict)]
    activity_items = list(activities)
    candidates: list[SessionMatch] = []
    for session in planned_items:
        for activity in activity_items:
            candidate = _candidate(session, activity, day_tolerance=day_tolerance)
            if candidate is not None and candidate.score >= minimum_score:
                candidates.append(candidate)

    candidates.sort(
        key=lambda item: (
            item.score,
            -item.day_difference,
            item.duration_ratio,
            item.name_similarity,
        ),
        reverse=True,
    )

    used_sessions: set[str] = set()
    used_activities: set[int] = set()
    matches: list[SessionMatch] = []
    for candidate in candidates:
        if candidate.session_key in used_sessions or candidate.activity_id in used_activities:
            continue
        used_sessions.add(candidate.session_key)
        used_activities.add(candidate.activity_id)
        matches.append(candidate)

    session_keys = {
        str(item.get("key") or item.get("session_key"))
        for item in planned_items
        if item.get("key") or item.get("session_key")
    }
    activity_ids = {
        identifier
        for item in activity_items
        if (identifier := _identifier(getattr(item, "activity_id", None))) is not None
    }
    return MatchResult(
        matches=tuple(sorted(matches, key=lambda item: item.session_key)),
        unmatched_session_keys=tuple(sorted(session_keys - used_sessions)),
        unmatched_activity_ids=tuple(sorted(activity_ids - used_activities)),
    )
