from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

LOG = logging.getLogger("garmin-activity-feedback")


@dataclass(frozen=True)
class ActivityFeedback:
    activity_id: int | None
    activity_day: str | None
    activity_name: str | None
    soreness: int | None = None
    fatigue: int | None = None
    motivation: int | None = None
    pain_or_injury: bool | None = None
    session_rpe: int | None = None
    source_text: str | None = None

    @property
    def has_values(self) -> bool:
        return any(
            value is not None
            for value in (
                self.soreness,
                self.fatigue,
                self.motivation,
                self.pain_or_injury,
                self.session_rpe,
            )
        )


NUMBER_ALIASES: dict[str, tuple[str, ...]] = {
    "soreness": (
        "soreness",
        "muscle soreness",
        "stiffness",
        "pijnspieren",
        "spierpijn",
        "stijfheid",
    ),
    "fatigue": ("fatigue", "tiredness", "vermoeidheid", "moeheid"),
    "motivation": ("motivation", "motivatie"),
    "session_rpe": (
        "session rpe",
        "rpe",
        "effort",
        "inspanning",
        "zwaarte",
    ),
}

BOOLEAN_ALIASES = (
    "pain/injury",
    "pain or injury",
    "pain",
    "injury",
    "pijn/blessure",
    "pijn of blessure",
    "blessure",
)

DESCRIPTION_KEYS = (
    "description",
    "activityDescription",
    "activity_description",
    "privateDescription",
    "private_description",
    "notes",
    "note",
    "activityNotes",
    "activity_notes",
    "comment",
    "comments",
)


def _clamp_score(value: int) -> int | None:
    return value if 0 <= value <= 10 else None


def _key_pattern(alias: str) -> str:
    words = [re.escape(item) for item in alias.split()]
    return r"[\s_-]*".join(words)


def _parse_number(text: str, aliases: Iterable[str]) -> int | None:
    for alias in aliases:
        pattern = rf"(?i)(?:^|[\s,;|#\[]){_key_pattern(alias)}\s*[:=\-]\s*(10|[0-9])(?:\b|/10)"
        match = re.search(pattern, text)
        if match:
            return _clamp_score(int(match.group(1)))
    return None


def _parse_boolean(text: str) -> bool | None:
    yes = {"yes", "y", "true", "1", "ja", "j"}
    no = {"no", "n", "false", "0", "nee"}
    for alias in BOOLEAN_ALIASES:
        pattern = rf"(?i)(?:^|[\s,;|#\[]){_key_pattern(alias)}\s*[:=\-]\s*([a-zA-Z]+|[01])\b"
        match = re.search(pattern, text)
        if not match:
            continue
        value = match.group(1).strip().lower()
        if value in yes:
            return True
        if value in no:
            return False
    return None


def parse_feedback_text(
    text: str | None,
    *,
    activity_id: int | None = None,
    activity_day: str | None = None,
    activity_name: str | None = None,
) -> ActivityFeedback:
    raw = (text or "").strip()
    values = {
        name: _parse_number(raw, aliases)
        for name, aliases in NUMBER_ALIASES.items()
    }
    store_raw = os.getenv("STORE_RAW_ACTIVITY_DESCRIPTION", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    return ActivityFeedback(
        activity_id=activity_id,
        activity_day=activity_day,
        activity_name=activity_name,
        soreness=values["soreness"],
        fatigue=values["fatigue"],
        motivation=values["motivation"],
        pain_or_injury=_parse_boolean(raw),
        session_rpe=values["session_rpe"],
        source_text=raw if raw and store_raw else None,
    )


def first_positive_int(obj: Any, names: Iterable[str]) -> int | None:
    wanted = set(names)
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in wanted:
                try:
                    parsed = int(value)
                except (TypeError, ValueError):
                    parsed = 0
                if parsed > 0:
                    return parsed
        for value in obj.values():
            nested = first_positive_int(value, wanted)
            if nested is not None:
                return nested
    elif isinstance(obj, list):
        for item in obj:
            nested = first_positive_int(item, wanted)
            if nested is not None:
                return nested
    return None


def activity_id(activity: dict[str, Any]) -> int | None:
    return first_positive_int(activity, ("activityId", "activity_id", "id"))


def activity_date(activity: dict[str, Any]) -> str | None:
    raw = (
        activity.get("startTimeLocal")
        or activity.get("startTimeGMT")
        or activity.get("calendarDate")
        or activity.get("activityDate")
    )
    if not raw:
        return None
    text = str(raw)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        try:
            return date.fromisoformat(text[:10]).isoformat()
        except ValueError:
            return None


def activity_name(activity: dict[str, Any]) -> str | None:
    for key in ("activityName", "name", "title"):
        value = activity.get(key)
        if value:
            return str(value)
    return None


def extract_description(payload: Any) -> str | None:
    """Extract the user-editable activity description/notes field.

    Garmin response shapes change. Direct known fields are preferred; a shallow
    recursive fallback accepts only keys whose names clearly indicate notes or
    descriptions. Workout-step descriptions are deliberately ignored.
    """
    if not isinstance(payload, dict):
        return None

    for key in DESCRIPTION_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    candidates: list[str] = []

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 4:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                normalized = str(key).lower().replace("_", "")
                looks_like_note = any(
                    token in normalized
                    for token in ("description", "note", "comment")
                )
                looks_like_workout_step = any(
                    token in normalized
                    for token in ("workoutstep", "stepdescription", "segment")
                )
                if (
                    looks_like_note
                    and not looks_like_workout_step
                    and isinstance(value, str)
                    and value.strip()
                ):
                    candidates.append(value.strip())
                elif isinstance(value, (dict, list)):
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node[:20]:
                walk(item, depth + 1)

    walk(payload)
    return max(candidates, key=len) if candidates else None


def _cache_path(data_dir: Path, activity_id_value: int) -> Path:
    return data_dir / "cache" / "activities" / f"{activity_id_value}.json"


def get_activity_payload(
    client: Any,
    summary: dict[str, Any],
    data_dir: Path,
    *,
    refresh_days: int = 3,
    today: date | None = None,
) -> dict[str, Any]:
    identifier = activity_id(summary)
    if identifier is None:
        return summary

    path = _cache_path(data_dir, identifier)
    when_raw = activity_date(summary)
    when = date.fromisoformat(when_raw) if when_raw else None
    today = today or date.today()
    refresh = when is not None and (today - when).days <= refresh_days

    if path.exists() and not refresh:
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(cached, dict):
                return {**summary, **cached}
        except (OSError, json.JSONDecodeError):
            LOG.warning("Ignoring invalid activity cache file %s", path)

    detailed: dict[str, Any] = {}
    try:
        response = client.get_activity(str(identifier))
        if isinstance(response, dict):
            detailed = response
    except Exception as exc:
        LOG.warning("Could not fetch Garmin activity %s: %s", identifier, exc)
        if path.exists():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(cached, dict):
                    detailed = cached
            except (OSError, json.JSONDecodeError):
                pass

    if detailed:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        safe_detail = cache_safe_activity_payload(detailed)
        temp.write_text(json.dumps(safe_detail, indent=2, default=str), encoding="utf-8")
        temp.replace(path)
    return {**summary, **detailed}


def feedback_from_activity(payload: dict[str, Any]) -> ActivityFeedback:
    cached = payload.get("_coach_feedback_cache")
    if isinstance(cached, dict):
        return ActivityFeedback(
            activity_id=activity_id(payload),
            activity_day=activity_date(payload),
            activity_name=activity_name(payload),
            soreness=_clamp_score(int(cached["soreness"])) if cached.get("soreness") is not None else None,
            fatigue=_clamp_score(int(cached["fatigue"])) if cached.get("fatigue") is not None else None,
            motivation=_clamp_score(int(cached["motivation"])) if cached.get("motivation") is not None else None,
            pain_or_injury=(
                bool(cached["pain_or_injury"])
                if cached.get("pain_or_injury") is not None
                else None
            ),
            session_rpe=_clamp_score(int(cached["session_rpe"])) if cached.get("session_rpe") is not None else None,
            source_text=None,
        )
    return parse_feedback_text(
        extract_description(payload),
        activity_id=activity_id(payload),
        activity_day=activity_date(payload),
        activity_name=activity_name(payload),
    )


def cache_safe_activity_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a cache copy with private free text removed.

    Parsed coach values are retained in a small structured field, but the full
    Garmin activity description, notes and comments are not written to disk.
    """
    parsed = feedback_from_activity(payload)

    def redact(node: Any) -> Any:
        if isinstance(node, dict):
            result: dict[str, Any] = {}
            for key, value in node.items():
                normalized = str(key).lower().replace("_", "")
                if any(token in normalized for token in ("description", "note", "comment")):
                    result[key] = None
                else:
                    result[key] = redact(value)
            return result
        if isinstance(node, list):
            return [redact(item) for item in node]
        return node

    safe = redact(payload)
    if not isinstance(safe, dict):
        safe = {}
    safe["_coach_feedback_cache"] = {
        "soreness": parsed.soreness,
        "fatigue": parsed.fatigue,
        "motivation": parsed.motivation,
        "pain_or_injury": parsed.pain_or_injury,
        "session_rpe": parsed.session_rpe,
    }
    return safe


def fetch_recent_feedback(
    client: Any,
    today: date,
    data_dir: Path,
    *,
    days: int = 4,
) -> list[ActivityFeedback]:
    start = today - timedelta(days=max(1, days) - 1)
    try:
        activities = client.get_activities_by_date(
            start.isoformat(), today.isoformat(), sortorder="desc"
        )
    except Exception as exc:
        LOG.warning("Could not read recent Garmin activities for feedback: %s", exc)
        return []

    results: list[ActivityFeedback] = []
    for summary in activities or []:
        if not isinstance(summary, dict):
            continue
        payload = get_activity_payload(client, summary, data_dir, today=today)
        feedback = feedback_from_activity(payload)
        if feedback.has_values:
            results.append(feedback)
    return sorted(
        results,
        key=lambda item: item.activity_day or "",
        reverse=True,
    )


def feedback_summary(items: list[ActivityFeedback]) -> dict[str, Any]:
    latest = items[0] if items else None
    latest_pain = next(
        (item.pain_or_injury for item in items if item.pain_or_injury is not None),
        None,
    )
    return {
        "count": len(items),
        "latest": asdict(latest) if latest else None,
        "pain_or_injury_reported": latest_pain,
        "max_soreness": max(
            (item.soreness for item in items if item.soreness is not None),
            default=None,
        ),
        "max_fatigue": max(
            (item.fatigue for item in items if item.fatigue is not None),
            default=None,
        ),
        "min_motivation": min(
            (item.motivation for item in items if item.motivation is not None),
            default=None,
        ),
        "max_session_rpe": max(
            (item.session_rpe for item in items if item.session_rpe is not None),
            default=None,
        ),
    }


def feedback_format_help() -> str:
    return (
        "Add this to the completed activity's private description in Garmin Connect: "
        "#coach soreness=3 fatigue=4 motivation=8 pain=no rpe=6"
    )
