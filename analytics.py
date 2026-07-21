from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any

from feedback import (
    ActivityFeedback,
    activity_date,
    activity_id,
    feedback_from_activity,
    get_activity_payload,
)

LOG = logging.getLogger("garmin-training-analytics")


@dataclass(frozen=True)
class ActivitySample:
    activity_id: int | None
    day: str
    name: str | None
    sport: str
    duration_minutes: int
    distance_m: float | None
    average_hr: float | None
    average_power: float | None
    normalized_power: float | None
    average_speed_mps: float | None
    aerobic_training_effect: float | None
    anaerobic_training_effect: float | None
    training_load: float | None
    vo2max: float | None
    efficiency_score: float | None
    feedback: ActivityFeedback


@dataclass
class WeeklySummary:
    week_start: str
    total_minutes: int = 0
    sessions: int = 0
    by_sport_minutes: dict[str, int] = field(
        default_factory=lambda: {"run": 0, "bike": 0, "swim": 0}
    )
    hard_sessions: int = 0
    pain_reports: int = 0
    average_rpe: float | None = None
    average_soreness: float | None = None
    average_fatigue: float | None = None
    average_motivation: float | None = None
    average_efficiency: dict[str, float | None] = field(default_factory=dict)


@dataclass
class TrainingAnalysis:
    generated_at: str
    period_start: str
    period_end: str
    activity_count: int
    total_minutes: int
    by_sport_minutes: dict[str, int]
    planned_sessions: int
    matched_sessions: int
    compliance: float | None
    completion_by_sport: dict[str, float | None]
    efficiency_change_percent: dict[str, float | None]
    recent_14d_minutes: int
    previous_14d_minutes: int
    volume_change_percent: float | None
    hard_sessions_recent_7d: int
    pain_reported: bool
    high_soreness_reported: bool
    high_fatigue_reported: bool
    low_motivation_reported: bool
    feedback_entries: int
    average_rpe: float | None
    recovery_red_days: int
    recovery_amber_days: int
    average_training_readiness: float | None
    average_sleep_score: float | None
    average_hrv_ratio: float | None
    current_week_index: int
    recovery_week: bool
    weekly_summaries: list[dict[str, Any]]
    samples: list[dict[str, Any]]
    structured_training_start_date: str | None = None
    training_age_weeks: int | None = None
    training_experience_band: str = "unknown"
    training_age_warning: str | None = None


SPORT_KEYS = {"run": "run", "running": "run", "bike": "bike", "cycling": "bike", "swim": "swim", "swimming": "swim"}


def _structured_training_context(
        today: date,
) -> tuple[str | None, int | None, str, str | None]:
    """Read optional structured-training age from the environment.

    Training age is context only. It never overrides current Garmin history,
    recovery, compliance, or pain feedback. Invalid values are ignored so a
    non-critical configuration typo cannot stop the daily recovery workflow.
    """
    raw = os.getenv("STRUCTURED_TRAINING_START_DATE", "").strip()
    if not raw:
        return None, None, "unknown", None

    try:
        started = date.fromisoformat(raw)
    except ValueError:
        warning = (
            "STRUCTURED_TRAINING_START_DATE must use YYYY-MM-DD; "
            f"ignoring {raw!r}."
        )
        LOG.warning(warning)
        return None, None, "unknown", warning

    if started > today:
        warning = (
            "STRUCTURED_TRAINING_START_DATE is in the future; "
            f"ignoring {started.isoformat()}."
        )
        LOG.warning(warning)
        return None, None, "unknown", warning

    weeks = max(0, (today - started).days // 7)
    if weeks < 8:
        band = "foundation"
    elif weeks < 24:
        band = "developing"
    elif weeks < 52:
        band = "established"
    else:
        band = "experienced"
    return started.isoformat(), weeks, band, None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _nested(payload: Any, *paths: tuple[str, ...]) -> Any:
    for path in paths:
        current = payload
        valid = True
        for key in path:
            if not isinstance(current, dict) or key not in current:
                valid = False
                break
            current = current[key]
        if valid and current is not None:
            return current
    return None


def sport_from_activity(activity: dict[str, Any]) -> str | None:
    raw = (
        _nested(activity, ("activityType", "typeKey"), ("sportType", "sportTypeKey"))
        or activity.get("activityTypeKey")
        or activity.get("sportType")
        or ""
    )
    text = str(raw).lower()
    if "run" in text or "treadmill" in text or "track" in text:
        return "run"
    if "cycl" in text or "bike" in text or "biking" in text:
        return "bike"
    if "swim" in text:
        return "swim"
    return None


def _duration_minutes(payload: dict[str, Any]) -> int:
    seconds = _number(
        payload.get("movingDuration")
        or payload.get("duration")
        or payload.get("elapsedDuration")
        or _nested(payload, ("summaryDTO", "duration"))
    )
    return max(0, int(round((seconds or 0) / 60)))


def _efficiency(sport: str, speed: float | None, hr: float | None, power: float | None) -> float | None:
    if sport == "bike" and power is not None and hr not in (None, 0):
        return round(power / hr, 5)
    if speed is not None and speed > 0 and hr not in (None, 0):
        return round(speed / hr, 7)
    if sport == "swim" and speed is not None and speed > 0:
        return round(speed, 5)
    return None


def sample_from_activity(payload: dict[str, Any]) -> ActivitySample | None:
    sport = sport_from_activity(payload)
    day = activity_date(payload)
    duration = _duration_minutes(payload)
    if sport is None or day is None or duration <= 0:
        return None

    distance = _number(payload.get("distance") or _nested(payload, ("summaryDTO", "distance")))
    avg_hr = _number(
        payload.get("averageHR")
        or payload.get("averageHeartRate")
        or _nested(payload, ("summaryDTO", "averageHR"))
    )
    avg_power = _number(
        payload.get("averagePower")
        or payload.get("avgPower")
        or _nested(payload, ("summaryDTO", "averagePower"))
    )
    normalized_power = _number(payload.get("normPower") or payload.get("normalizedPower"))
    speed = _number(
        payload.get("averageSpeed")
        or payload.get("avgSpeed")
        or _nested(payload, ("summaryDTO", "averageSpeed"))
    )
    feedback = feedback_from_activity(payload)
    return ActivitySample(
        activity_id=activity_id(payload),
        day=day,
        name=str(payload.get("activityName") or payload.get("name") or "") or None,
        sport=sport,
        duration_minutes=duration,
        distance_m=distance,
        average_hr=avg_hr,
        average_power=avg_power,
        normalized_power=normalized_power,
        average_speed_mps=speed,
        aerobic_training_effect=_number(payload.get("aerobicTrainingEffect")),
        anaerobic_training_effect=_number(payload.get("anaerobicTrainingEffect")),
        training_load=_number(payload.get("activityTrainingLoad") or payload.get("trainingLoad")),
        vo2max=_number(
            payload.get("vO2MaxValue")
            or payload.get("vo2MaxValue")
            or payload.get("vo2Max")
        ),
        efficiency_score=_efficiency(sport, speed, avg_hr, avg_power),
        feedback=feedback,
    )


def fetch_activity_samples(
    client: Any,
    today: date,
    days: int,
    data_dir: Path,
) -> list[ActivitySample]:
    start = today - timedelta(days=max(1, days) - 1)
    try:
        activities = client.get_activities_by_date(
            start.isoformat(), today.isoformat(), sortorder="asc"
        )
    except Exception as exc:
        LOG.warning("Could not fetch Garmin activities for analysis: %s", exc)
        return []

    samples: list[ActivitySample] = []
    for summary in activities or []:
        if not isinstance(summary, dict):
            continue
        payload = get_activity_payload(client, summary, data_dir, today=today)
        sample = sample_from_activity(payload)
        if sample is not None:
            samples.append(sample)
    return sorted(samples, key=lambda item: (item.day, item.activity_id or 0))


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _mean(values: list[float]) -> float | None:
    return round(mean(values), 3) if values else None


def weekly_summaries(samples: list[ActivitySample]) -> list[WeeklySummary]:
    buckets: dict[str, list[ActivitySample]] = {}
    for sample in samples:
        key = monday_of(date.fromisoformat(sample.day)).isoformat()
        buckets.setdefault(key, []).append(sample)

    results: list[WeeklySummary] = []
    for week_start, items in sorted(buckets.items()):
        result = WeeklySummary(week_start=week_start)
        rpe_values: list[float] = []
        soreness_values: list[float] = []
        fatigue_values: list[float] = []
        motivation_values: list[float] = []
        efficiency: dict[str, list[float]] = {"run": [], "bike": [], "swim": []}
        for item in items:
            result.total_minutes += item.duration_minutes
            result.sessions += 1
            result.by_sport_minutes[item.sport] += item.duration_minutes
            if (
                (item.aerobic_training_effect or 0) >= 3.5
                or (item.anaerobic_training_effect or 0) >= 2.0
                or (item.feedback.session_rpe or 0) >= 8
            ):
                result.hard_sessions += 1
            if item.feedback.pain_or_injury is True:
                result.pain_reports += 1
            if item.feedback.session_rpe is not None:
                rpe_values.append(float(item.feedback.session_rpe))
            if item.feedback.soreness is not None:
                soreness_values.append(float(item.feedback.soreness))
            if item.feedback.fatigue is not None:
                fatigue_values.append(float(item.feedback.fatigue))
            if item.feedback.motivation is not None:
                motivation_values.append(float(item.feedback.motivation))
            if item.efficiency_score is not None:
                efficiency[item.sport].append(item.efficiency_score)
        result.average_rpe = _mean(rpe_values)
        result.average_soreness = _mean(soreness_values)
        result.average_fatigue = _mean(fatigue_values)
        result.average_motivation = _mean(motivation_values)
        result.average_efficiency = {
            sport: _mean(values) for sport, values in efficiency.items()
        }
        results.append(result)
    return results


def _period_samples(samples: list[ActivitySample], start: date, end: date) -> list[ActivitySample]:
    return [
        item
        for item in samples
        if start <= date.fromisoformat(item.day) <= end
    ]


def _efficiency_change(samples: list[ActivitySample], today: date, sport: str) -> float | None:
    recent = [
        item.efficiency_score
        for item in _period_samples(samples, today - timedelta(days=13), today)
        if item.sport == sport and item.efficiency_score is not None
    ]
    previous = [
        item.efficiency_score
        for item in _period_samples(
            samples, today - timedelta(days=27), today - timedelta(days=14)
        )
        if item.sport == sport and item.efficiency_score is not None
    ]
    if len(recent) < 2 or len(previous) < 2:
        return None
    previous_mean = mean(previous)
    if previous_mean == 0:
        return None
    return round((mean(recent) / previous_mean - 1) * 100, 2)


def _load_planner_state(data_dir: Path) -> dict[str, Any]:
    path = data_dir / "planner_state.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _planned_records(state: dict[str, Any], start: date, end: date) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for record in (state.get("sessions") or {}).values():
        if not isinstance(record, dict):
            continue
        session = record.get("session") or {}
        raw_day = session.get("day")
        if not raw_day:
            continue
        try:
            when = date.fromisoformat(str(raw_day))
        except ValueError:
            continue
        if start <= when <= end and record.get("status") not in {"cancelled"}:
            results.append(session)
    return results


def _match_compliance(
    planned: list[dict[str, Any]], samples: list[ActivitySample]
) -> tuple[int, dict[str, tuple[int, int]]]:
    by_key: dict[tuple[str, str], list[ActivitySample]] = {}
    for sample in samples:
        by_key.setdefault((sample.day, sample.sport), []).append(sample)
    for values in by_key.values():
        values.sort(key=lambda item: item.duration_minutes, reverse=True)

    matched = 0
    by_sport = {"run": [0, 0], "bike": [0, 0], "swim": [0, 0]}
    for session in sorted(planned, key=lambda item: (item.get("day", ""), item.get("slot_index", 0))):
        sport = str(session.get("sport", ""))
        if sport not in by_sport:
            continue
        by_sport[sport][1] += 1
        candidates = by_key.get((str(session.get("day")), sport), [])
        planned_minutes = max(1, int(session.get("duration_minutes") or 0))
        index = next(
            (
                idx
                for idx, candidate in enumerate(candidates)
                if candidate.duration_minutes >= max(10, planned_minutes * 0.55)
            ),
            None,
        )
        if index is not None:
            candidates.pop(index)
            matched += 1
            by_sport[sport][0] += 1
    return matched, {key: (value[0], value[1]) for key, value in by_sport.items()}


def _read_recovery_reports(data_dir: Path, start: date, end: date) -> dict[str, Any]:
    red = 0
    amber = 0
    readiness: list[float] = []
    sleep: list[float] = []
    hrv: list[float] = []
    report_dir = data_dir / "reports"
    if not report_dir.exists():
        return {
            "red": 0,
            "amber": 0,
            "readiness": None,
            "sleep": None,
            "hrv": None,
        }
    for path in report_dir.glob("*.json"):
        try:
            when = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if not (start <= when <= end):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        status = str((payload.get("advice") or {}).get("status", "")).upper()
        red += status == "RED"
        amber += status == "AMBER"
        metrics = payload.get("metrics") or {}
        for target, key in (
            (readiness, "training_readiness"),
            (sleep, "sleep_score"),
            (hrv, "hrv_ratio"),
        ):
            value = _number(metrics.get(key))
            if value is not None:
                target.append(value)
    return {
        "red": red,
        "amber": amber,
        "readiness": _mean(readiness),
        "sleep": _mean(sleep),
        "hrv": _mean(hrv),
    }


def build_training_analysis(
    client: Any,
    today: date,
    data_dir: Path,
    *,
    days: int = 42,
) -> TrainingAnalysis:
    samples = fetch_activity_samples(client, today, days, data_dir)
    start = today - timedelta(days=days - 1)
    recent_start = today - timedelta(days=13)
    previous_start = today - timedelta(days=27)
    previous_end = today - timedelta(days=14)
    recent_samples = _period_samples(samples, recent_start, today)
    previous_samples = _period_samples(samples, previous_start, previous_end)

    state = _load_planner_state(data_dir)
    compliance_start = today - timedelta(days=27)
    planned = _planned_records(state, compliance_start, today)
    compliance_samples = _period_samples(samples, compliance_start, today)
    matched, by_sport_matches = _match_compliance(planned, compliance_samples)
    compliance = round(matched / len(planned), 3) if planned else None
    completion_by_sport = {
        sport: (round(matched_count / planned_count, 3) if planned_count else None)
        for sport, (matched_count, planned_count) in by_sport_matches.items()
    }

    feedback_items = [sample.feedback for sample in samples if sample.feedback.has_values]
    recent_feedback_items = [
        sample.feedback
        for sample in _period_samples(samples, today - timedelta(days=13), today)
        if sample.feedback.has_values
    ]
    latest_pain = next(
        (
            item.pain_or_injury
            for item in reversed(recent_feedback_items)
            if item.pain_or_injury is not None
        ),
        None,
    )
    rpes = [float(item.session_rpe) for item in feedback_items if item.session_rpe is not None]
    recent_7d = _period_samples(samples, today - timedelta(days=6), today)
    hard_recent = sum(
        1
        for item in recent_7d
        if (item.aerobic_training_effect or 0) >= 3.5
        or (item.anaerobic_training_effect or 0) >= 2.0
        or (item.feedback.session_rpe or 0) >= 8
    )

    recovery = _read_recovery_reports(data_dir, today - timedelta(days=27), today)
    plan_started = state.get("plan_started_on")
    try:
        week_index = max(
            0,
            (monday_of(today) - monday_of(date.fromisoformat(str(plan_started)))).days // 7,
        )
    except (TypeError, ValueError):
        week_index = 0

    recent_minutes = sum(item.duration_minutes for item in recent_samples)
    previous_minutes = sum(item.duration_minutes for item in previous_samples)
    volume_change = (
        round((recent_minutes / previous_minutes - 1) * 100, 2)
        if previous_minutes > 0
        else None
    )
    by_sport_minutes = {
        sport: sum(item.duration_minutes for item in samples if item.sport == sport)
        for sport in ("run", "bike", "swim")
    }
    summaries = weekly_summaries(samples)
    (
        structured_training_start_date,
        training_age_weeks,
        training_experience_band,
        training_age_warning,
    ) = _structured_training_context(today)

    return TrainingAnalysis(
        generated_at=datetime.now().astimezone().isoformat(),
        period_start=start.isoformat(),
        period_end=today.isoformat(),
        activity_count=len(samples),
        total_minutes=sum(item.duration_minutes for item in samples),
        by_sport_minutes=by_sport_minutes,
        planned_sessions=len(planned),
        matched_sessions=matched,
        compliance=compliance,
        completion_by_sport=completion_by_sport,
        efficiency_change_percent={
            sport: _efficiency_change(samples, today, sport)
            for sport in ("run", "bike", "swim")
        },
        recent_14d_minutes=recent_minutes,
        previous_14d_minutes=previous_minutes,
        volume_change_percent=volume_change,
        hard_sessions_recent_7d=hard_recent,
        pain_reported=latest_pain is True,
        high_soreness_reported=any(
            (item.soreness or 0) >= 7 for item in recent_feedback_items[-6:]
        ),
        high_fatigue_reported=any(
            (item.fatigue or 0) >= 7 for item in recent_feedback_items[-6:]
        ),
        low_motivation_reported=any(
            item.motivation is not None and item.motivation <= 3
            for item in recent_feedback_items[-6:]
        ),
        feedback_entries=len(feedback_items),
        average_rpe=_mean(rpes),
        recovery_red_days=int(recovery["red"]),
        recovery_amber_days=int(recovery["amber"]),
        average_training_readiness=recovery["readiness"],
        average_sleep_score=recovery["sleep"],
        average_hrv_ratio=recovery["hrv"],
        current_week_index=week_index,
        recovery_week=week_index % 4 == 3,
        weekly_summaries=[asdict(item) for item in summaries],
        samples=[asdict(item) for item in samples],
        structured_training_start_date=structured_training_start_date,
        training_age_weeks=training_age_weeks,
        training_experience_band=training_experience_band,
        training_age_warning=training_age_warning,
    )


def save_analysis(analysis: TrainingAnalysis, data_dir: Path) -> Path:
    path = data_dir / "analytics" / f"{analysis.period_end}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(analysis), indent=2, default=str), encoding="utf-8")
    return path
