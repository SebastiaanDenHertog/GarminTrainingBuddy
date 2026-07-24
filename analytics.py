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

from benchmarks import due_status, evaluate_result
from matching import MatchResult, match_planned_sessions
from training_db import (
    database_enabled,
    import_recovery_reports,
    replace_matches,
    save_analysis_snapshot,
    summary as database_summary,
    sync_planner_state,
    upsert_activities,
    upsert_benchmark_result,
)

from feedback import (
    ActivityFeedback,
    activity_date,
    activity_id,
    feedback_from_activity,
    first_positive_int,
    get_activity_payload,
)

LOG = logging.getLogger("garmin-training-analytics")


@dataclass(frozen=True)
class ActivitySample:
    activity_id: int | None
    day: str
    start_time_local: str | None
    name: str | None
    sport: str
    duration_minutes: int
    distance_m: float | None
    average_hr: float | None
    max_hr: float | None
    average_power: float | None
    normalized_power: float | None
    max_power: float | None
    average_speed_mps: float | None
    aerobic_training_effect: float | None
    anaerobic_training_effect: float | None
    training_load: float | None
    vo2max: float | None
    efficiency_score: float | None
    workout_id: int | None
    feedback: ActivityFeedback


@dataclass
class WeeklySummary:
    week_start: str
    total_minutes: int = 0
    sessions: int = 0
    by_sport_minutes: dict[str, int] = field(
        default_factory=lambda: {"run": 0, "bike": 0, "swim": 0, "strength": 0}
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
    match_quality: dict[str, int] = field(default_factory=dict)
    unmatched_planned_sessions: int = 0
    unmatched_activities: int = 0
    performance_trends: dict[str, dict[str, Any]] = field(default_factory=dict)
    recovery_trend: dict[str, Any] = field(default_factory=dict)
    database: dict[str, Any] = field(default_factory=dict)
    benchmark_status: list[dict[str, Any]] = field(default_factory=list)


SPORT_KEYS = {"run": "run", "running": "run", "bike": "bike", "cycling": "bike", "swim": "swim", "swimming": "swim", "strength": "strength", "strength_training": "strength"}


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
    if "strength" in text or "weight" in text or "resistance" in text:
        return "strength"
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
        start_time_local=(
            str(payload.get("startTimeLocal"))
            if payload.get("startTimeLocal") is not None
            else None
        ),
        name=str(payload.get("activityName") or payload.get("name") or "") or None,
        sport=sport,
        duration_minutes=duration,
        distance_m=distance,
        average_hr=avg_hr,
        max_hr=_number(payload.get("maxHR") or payload.get("maxHeartRate")),
        average_power=avg_power,
        normalized_power=normalized_power,
        max_power=_number(payload.get("maxPower")),
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
        workout_id=first_positive_int(
            payload,
            ("workoutId", "workout_id", "trainingPlanWorkoutId"),
        ),
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
        efficiency: dict[str, list[float]] = {"run": [], "bike": [], "swim": [], "strength": []}
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
            item = dict(session)
            item["_record_workout_id"] = record.get("workout_id")
            item["_record_scheduled_workout_id"] = record.get("scheduled_workout_id")
            item["_record_status"] = record.get("status")
            results.append(item)
    return results


def _match_compliance(
    planned: list[dict[str, Any]], samples: list[ActivitySample],
    *,
    day_tolerance: int = 1,
    minimum_score: float = 55.0,
) -> tuple[int, dict[str, tuple[int, int]], MatchResult]:
    result = match_planned_sessions(
        planned,
        samples,
        day_tolerance=day_tolerance,
        minimum_score=minimum_score,
    )
    planned_by_key = {str(item.get("key")): item for item in planned}
    by_sport = {"run": [0, 0], "bike": [0, 0], "swim": [0, 0], "strength": [0, 0]}
    for session in planned:
        sport = str(session.get("sport") or "")
        if sport in by_sport:
            by_sport[sport][1] += 1
    for match in result.matches:
        sport = str((planned_by_key.get(match.session_key) or {}).get("sport") or "")
        if sport in by_sport:
            by_sport[sport][0] += 1
    return (
        len(result.matches),
        {key: (value[0], value[1]) for key, value in by_sport.items()},
        result,
    )


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


def _period_metric_change(
    samples: list[ActivitySample],
    today: date,
    sport: str,
    attribute: str,
) -> tuple[float | None, float | None, float | None, int, int]:
    recent_items = [
        item for item in _period_samples(samples, today - timedelta(days=13), today)
        if item.sport == sport and getattr(item, attribute, None) is not None
    ]
    previous_items = [
        item for item in _period_samples(
            samples, today - timedelta(days=27), today - timedelta(days=14)
        )
        if item.sport == sport and getattr(item, attribute, None) is not None
    ]
    recent_values = [float(getattr(item, attribute)) for item in recent_items]
    previous_values = [float(getattr(item, attribute)) for item in previous_items]
    recent_mean = _mean(recent_values)
    previous_mean = _mean(previous_values)
    change = None
    if recent_mean is not None and previous_mean not in (None, 0):
        change = round((recent_mean / previous_mean - 1) * 100, 2)
    return recent_mean, previous_mean, change, len(recent_values), len(previous_values)


def _performance_trends(samples: list[ActivitySample], today: date) -> dict[str, dict[str, Any]]:
    trends: dict[str, dict[str, Any]] = {}
    for sport in ("run", "bike", "swim"):
        efficiency = _period_metric_change(samples, today, sport, "efficiency_score")
        vo2 = _period_metric_change(samples, today, sport, "vo2max")
        recent_volume = sum(
            item.duration_minutes
            for item in _period_samples(samples, today - timedelta(days=13), today)
            if item.sport == sport
        )
        previous_volume = sum(
            item.duration_minutes
            for item in _period_samples(
                samples, today - timedelta(days=27), today - timedelta(days=14)
            )
            if item.sport == sport
        )
        volume_change = (
            round((recent_volume / previous_volume - 1) * 100, 2)
            if previous_volume > 0
            else None
        )
        efficiency_change = efficiency[2]
        vo2_change = None
        if vo2[0] is not None and vo2[1] is not None:
            vo2_change = round(vo2[0] - vo2[1], 2)
        sample_count = min(efficiency[3] + vo2[3], efficiency[4] + vo2[4])
        if efficiency[3] < 2 or efficiency[4] < 2:
            status = "insufficient_data"
            confidence = 0.25 if recent_volume else 0.0
        elif (efficiency_change or 0) >= 1.5 or (vo2_change or 0) >= 0.5:
            status = "improving"
            confidence = min(0.95, 0.55 + 0.06 * sample_count)
        elif (efficiency_change or 0) <= -2.0 or (vo2_change or 0) <= -0.5:
            status = "declining"
            confidence = min(0.95, 0.55 + 0.06 * sample_count)
        else:
            status = "stable"
            confidence = min(0.9, 0.5 + 0.05 * sample_count)
        trends[sport] = {
            "status": status,
            "confidence": round(confidence, 2),
            "recent_14d_minutes": recent_volume,
            "previous_14d_minutes": previous_volume,
            "volume_change_percent": volume_change,
            "efficiency_recent": efficiency[0],
            "efficiency_previous": efficiency[1],
            "efficiency_change_percent": efficiency_change,
            "vo2max_recent": vo2[0],
            "vo2max_previous": vo2[1],
            "vo2max_change": vo2_change,
            "recent_samples": max(efficiency[3], vo2[3]),
            "previous_samples": max(efficiency[4], vo2[4]),
        }
    return trends


def _recovery_trend(data_dir: Path, today: date) -> dict[str, Any]:
    recent = _read_recovery_reports(data_dir, today - timedelta(days=6), today)
    previous = _read_recovery_reports(
        data_dir, today - timedelta(days=13), today - timedelta(days=7)
    )
    deltas: dict[str, float | None] = {}
    for key in ("readiness", "sleep", "hrv"):
        deltas[key] = (
            round(float(recent[key]) - float(previous[key]), 3)
            if recent.get(key) is not None and previous.get(key) is not None
            else None
        )
    worsening = (
        (deltas["readiness"] is not None and deltas["readiness"] <= -8)
        or (deltas["sleep"] is not None and deltas["sleep"] <= -8)
        or (deltas["hrv"] is not None and deltas["hrv"] <= -0.08)
        or int(recent["red"]) > int(previous["red"])
    )
    improving = (
        not worsening
        and (
            (deltas["readiness"] is not None and deltas["readiness"] >= 5)
            or (deltas["sleep"] is not None and deltas["sleep"] >= 5)
            or (deltas["hrv"] is not None and deltas["hrv"] >= 0.05)
        )
    )
    return {
        "status": "worsening" if worsening else ("improving" if improving else "stable"),
        "recent_7d": recent,
        "previous_7d": previous,
        "delta": deltas,
    }


def build_training_analysis(
    client: Any,
    today: date,
    data_dir: Path,
    *,
    days: int = 42,
    config: dict[str, Any] | None = None,
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
    measurement = (config or {}).get("measurement") or {}
    day_tolerance = int(measurement.get("match_day_tolerance", 1))
    minimum_match_score = float(measurement.get("minimum_match_score", 55))
    matched, by_sport_matches, match_result = _match_compliance(
        planned,
        compliance_samples,
        day_tolerance=day_tolerance,
        minimum_score=minimum_match_score,
    )
    compliance = round(matched / len(planned), 3) if planned else None
    completion_by_sport = {
        sport: (round(matched_count / planned_count, 3) if planned_count else None)
        for sport, (matched_count, planned_count) in by_sport_matches.items()
    }

    if database_enabled():
        sync_planner_state(data_dir, state)
        upsert_activities(data_dir, samples)
        import_recovery_reports(data_dir)
        replace_matches(data_dir, match_result)
        planned_by_key = {str(item.get("key")): item for item in planned}
        samples_by_id = {int(item.activity_id): item for item in samples if item.activity_id}
        for match in match_result.matches:
            session = planned_by_key.get(match.session_key)
            sample = samples_by_id.get(match.activity_id)
            if not session or not sample or not session.get("benchmark_id"):
                continue
            result = evaluate_result(
                data_dir,
                session,
                sample,
                pool_length_m=int((config or {}).get("plan", {}).get("pool_length_m", 25)),
            )
            if result:
                upsert_benchmark_result(data_dir, result)

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
        for sport in ("run", "bike", "swim", "strength")
    }
    summaries = weekly_summaries(samples)
    (
        structured_training_start_date,
        training_age_weeks,
        training_experience_band,
        training_age_warning,
    ) = _structured_training_context(today)
    performance_trends = _performance_trends(samples, today)
    recovery_trend = _recovery_trend(data_dir, today)
    db_info = database_summary(data_dir) if database_enabled() else {"enabled": False}
    benchmark_status = due_status(
        config or {},
        db_info.get("latest_benchmarks") or [],
        today,
    )
    quality = {"high": 0, "medium": 0, "low": 0}
    for match in match_result.matches:
        if match.score >= 85:
            quality["high"] += 1
        elif match.score >= 70:
            quality["medium"] += 1
        else:
            quality["low"] += 1

    analysis = TrainingAnalysis(
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
        match_quality=quality,
        unmatched_planned_sessions=len(match_result.unmatched_session_keys),
        unmatched_activities=len(match_result.unmatched_activity_ids),
        performance_trends=performance_trends,
        recovery_trend=recovery_trend,
        database=db_info,
        benchmark_status=benchmark_status,
    )
    if database_enabled():
        save_analysis_snapshot(data_dir, analysis)
    return analysis


def save_analysis(analysis: TrainingAnalysis, data_dir: Path) -> Path:
    path = data_dir / "analytics" / f"{analysis.period_end}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(analysis), indent=2, default=str), encoding="utf-8")
    return path
