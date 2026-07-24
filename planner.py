from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import yaml
from garminconnect import Garmin

from coach import apply_adjustment_to_config
from benchmarks import apply_due_benchmark
from targets import annotate_sessions, resolve_training_targets
from garminconnect.workout import (
    BaseWorkout,
    ConditionType,
    CyclingWorkout,
    ExecutableStep,
    RepeatGroup,
    RunningWorkout,
    SportType,
    StepType,
    SwimmingWorkout,
    TargetType,
    WorkoutSegment,
    create_cooldown_step,
    create_interval_step,
    create_recovery_step,
    create_repeat_group,
    create_warmup_step,
)

LOG = logging.getLogger("garmin-adaptive-planner")

DAY_INDEX = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

SPORT_TYPE = {
    "run": {"sportTypeId": SportType.RUNNING, "sportTypeKey": "running", "displayOrder": 1},
    "bike": {"sportTypeId": SportType.CYCLING, "sportTypeKey": "cycling", "displayOrder": 2},
    "swim": {"sportTypeId": SportType.SWIMMING, "sportTypeKey": "swimming", "displayOrder": 3},
    "strength": {
        "sportTypeId": SportType.STRENGTH_TRAINING,
        "sportTypeKey": "strength_training",
        "displayOrder": 5,
    },
}

ENDURANCE_SPORTS = {"run", "bike", "swim"}

SEVERITY = {"UNKNOWN": 0, "GREEN": 0, "AMBER": 1, "RED": 2}


def is_missing_workout_schedule_error(exc: Exception) -> bool:
    """Return True only for Garmin's idempotent missing-schedule response.

    A rebuild can be interrupted after Garmin removes an entry but before the
    updated local state is saved. The next rebuild then sees the stale schedule
    ID and Garmin correctly returns HTTP 404. Treat that one precise response as
    "already removed" while re-raising every other API failure.
    """
    message = str(exc).strip().lower()
    return (
            "404" in message
            and "no workout found for workout schedule" in message
    )


@dataclass(frozen=True)
class Session:
    key: str
    day: str
    sport: str
    role: str
    duration_minutes: int
    name: str
    description: str
    intensity: str
    priority: int
    phase: str
    week_index: int
    slot_index: int
    scheduled_time: str | None = None
    primary: bool = True
    after: str | None = None
    minimum_gap_hours: float | None = None
    max_gap_minutes: int | None = None
    strength_template: str | None = None
    strength_exercises: tuple[dict[str, Any], ...] = ()
    benchmark_id: str | None = None
    target_kind: str | None = None
    target_low: float | None = None
    target_high: float | None = None
    target_unit: str | None = None
    target_summary: str | None = None
    target_source: str | None = None

    def fingerprint(self) -> str:
        payload = asdict(self)
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]


@dataclass(frozen=True)
class RecentLoad:
    days: int
    total_minutes: int
    weekly_average_minutes: int | None
    active_weeks: int
    by_sport_minutes: dict[str, int]


class PlannerConfigError(ValueError):
    pass


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


DEFAULT_CONFIG: dict[str, Any] = {
    "plan": {
        "name_prefix": "AI BASE",
        "horizon_days": 14,
        "publish_weekday": "sunday",
        "publish_weeks": 2,
        "publish_bootstrap_when_empty": True,
        "plan_length_weeks": 52,
        "base_weekly_minutes": 360,
        "min_weekly_minutes": 240,
        "max_weekly_minutes": 660,
        "block_growth": 0.04,
        "recovery_week_factor": 0.72,
        "use_recent_garmin_load": True,
        "recent_load_days": 28,
        "max_hard_sessions_per_week": 2,
        "max_sessions_per_day": 2,
        "max_hard_sessions_per_day": 1,
        "coach_volume_multiplier": 1.0,
        "sport_weight_multipliers": {
            "run": 1.0,
            "bike": 1.0,
            "swim": 1.0,
            "strength": 1.0,
        },
        "quality_progression": {"run": 0, "bike": 0, "swim": 0},
        "max_strength_sessions_per_week": 2,
        "amber_duration_factor": 0.72,
        "red_duration_minutes": 30,
        "pool_length_m": 25,
        "swim_meters_per_hour": 2200,
    },
    "workout_targets": {
        "enabled": True,
        "benchmark_conversions": {
            "minimum_confidence": 0.65,
            "run_20min_to_threshold_pace_factor": 1.03,
            "bike_20min_to_ftp_factor": 0.95,
        },
        "manual": {
            "run_threshold_pace": None,
            "run_threshold_hr_bpm": None,
            "bike_ftp_watts": None,
            "bike_threshold_hr_bpm": None,
            "swim_css_pace": None,
        },
        "ranges": {
            "run_recovery": {
                "hr_pct_lthr": [0.70, 0.80],
                "pace_multiplier": [1.28, 1.48],
            },
            "run_easy": {
                "hr_pct_lthr": [0.75, 0.85],
                "pace_multiplier": [1.20, 1.40],
            },
            "run_brick": {
                "hr_pct_lthr": [0.76, 0.86],
                "pace_multiplier": [1.18, 1.38],
            },
            "run_long": {
                "hr_pct_lthr": [0.78, 0.88],
                "pace_multiplier": [1.15, 1.35],
            },
            "run_quality": {
                "foundation": {
                    "pace_multiplier": [0.94, 1.02],
                    "hr_pct_lthr": [0.90, 0.98],
                },
                "aerobic_build": {
                    "pace_multiplier": [0.98, 1.04],
                    "hr_pct_lthr": [0.94, 1.00],
                },
                "vo2_economy": {
                    "pace_multiplier": [0.88, 0.95],
                    "hr_pct_lthr": [0.95, 1.02],
                },
                "durability": {
                    "pace_multiplier": [0.97, 1.02],
                    "hr_pct_lthr": [0.90, 0.97],
                },
            },
            "bike_easy": {
                "hr_pct_lthr": [0.72, 0.84],
                "ftp_pct": [0.55, 0.72],
            },
            "bike_long": {
                "hr_pct_lthr": [0.75, 0.87],
                "ftp_pct": [0.60, 0.76],
            },
            "bike_quality": {
                "foundation": {
                    "ftp_pct": [0.88, 1.00],
                    "hr_pct_lthr": [0.88, 0.97],
                },
                "aerobic_build": {
                    "ftp_pct": [0.95, 1.02],
                    "hr_pct_lthr": [0.92, 1.00],
                },
                "vo2_economy": {
                    "ftp_pct": [1.06, 1.18],
                    "hr_pct_lthr": [0.95, 1.02],
                },
                "durability": {
                    "ftp_pct": [0.88, 0.96],
                    "hr_pct_lthr": [0.88, 0.96],
                },
            },
        },
    },
    "strength_templates": {
        "full_body_a": {
            "rounds": 3,
            "target_rpe": 7,
            "rest_seconds": 90,
            "exercises": [
                {"name": "Goblet squat", "reps": "6-8"},
                {"name": "Romanian deadlift", "reps": "6-8"},
                {"name": "Single-arm row", "reps": "8 each side"},
                {"name": "Push-up or dumbbell press", "reps": "8-12"},
                {"name": "Calf raise", "reps": "10-15"},
                {"name": "Dead bug or Pallof press", "reps": "8 each side"},
            ],
        },
        "full_body_b": {
            "rounds": 3,
            "target_rpe": 7,
            "rest_seconds": 90,
            "exercises": [
                {"name": "Split squat", "reps": "6-8 each side"},
                {"name": "Hip thrust", "reps": "8-10"},
                {"name": "Lat pulldown or pull-up", "reps": "6-10"},
                {"name": "Overhead press", "reps": "6-10"},
                {"name": "Bent-knee soleus raise", "reps": "12-15"},
                {"name": "Side plank", "reps": "30-45 seconds each side"},
            ],
        },
    },
    "measurement": {
        "history_days": 180,
        "match_day_tolerance": 1,
        "minimum_match_score": 55,
        "benchmarks": {
            "enabled": False,
            "auto_schedule": False,
            "interval_weeks": 8,
            "first_test_week": 2,
            "sports": {
                "run": {
                    "protocol": "run_aerobic_30",
                    "offset_weeks": 0,
                    "preferred_weekday": "thursday",
                    "replace_roles": ["quality", "easy"],
                },
                "bike": {
                    "protocol": "bike_aerobic_45",
                    "offset_weeks": 2,
                    "preferred_weekday": "tuesday",
                    "replace_roles": ["quality", "endurance", "easy"],
                },
                "swim": {
                    "protocol": "swim_css_400_200",
                    "offset_weeks": 3,
                    "preferred_weekday": "friday",
                    "replace_roles": ["quality", "endurance"],
                },
            },
        },
    },
    "training_days": {
        "monday": [],
        "tuesday": [
            {"sport": "swim", "role": "technique", "weight": 0.85, "min_minutes": 35, "max_minutes": 65,
             "time": "06:30", "primary": True}
        ],
        "wednesday": [
            {"sport": "bike", "role": "easy", "weight": 0.85, "min_minutes": 40, "max_minutes": 75, "time": "17:30",
             "primary": True},
            {"sport": "strength", "role": "strength", "template": "full_body_a", "fixed_minutes": 40, "min_minutes": 30,
             "max_minutes": 50, "time": "19:00", "primary": False, "minimum_gap_hours": 1.0},
        ],
        "thursday": [
            {"sport": "run", "role": "easy", "weight": 0.85, "min_minutes": 35, "max_minutes": 65, "time": "17:30",
             "primary": True}
        ],
        "friday": [
            {"sport": "swim", "role": "endurance", "weight": 0.90, "min_minutes": 40, "max_minutes": 70,
             "time": "06:30", "primary": True}
        ],
        "saturday": [
            {"sport": "bike", "role": "long", "weight": 1.55, "min_minutes": 70, "max_minutes": 180, "time": "08:00",
             "primary": True},
            {"sport": "run", "role": "brick", "weight": 0.35, "min_minutes": 15, "max_minutes": 35, "time": "11:00",
             "primary": False, "after": "bike", "max_gap_minutes": 30},
        ],
        "sunday": [
            {"sport": "run", "role": "long", "weight": 1.25, "min_minutes": 55, "max_minutes": 120, "time": "09:00",
             "primary": True}
        ],
    },
}


def _apply_publication_env_overrides(config: dict[str, Any]) -> None:
    plan = config.setdefault("plan", {})
    weekday = os.getenv("PLAN_PUBLISH_WEEKDAY")
    weeks = os.getenv("PLAN_PUBLISH_WEEKS")
    bootstrap = os.getenv("PLAN_PUBLISH_BOOTSTRAP_WHEN_EMPTY")
    if weekday not in (None, ""):
        plan["publish_weekday"] = weekday.strip().lower()
    if weeks not in (None, ""):
        try:
            plan["publish_weeks"] = int(weeks)
        except ValueError as exc:
            raise PlannerConfigError("PLAN_PUBLISH_WEEKS must be an integer.") from exc
    if bootstrap not in (None, ""):
        plan["publish_bootstrap_when_empty"] = bootstrap.strip().lower() in {
            "1", "true", "yes", "on"
        }
    plan["horizon_days"] = max(1, int(plan.get("publish_weeks", 2))) * 7


def load_plan_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        raise PlannerConfigError(f"Plan config does not exist: {config_path}")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise PlannerConfigError("The YAML root must be a mapping.")
    config = deep_merge(DEFAULT_CONFIG, raw)
    _apply_publication_env_overrides(config)
    validate_config(config)
    return config


def validate_config(config: dict[str, Any]) -> None:
    plan = config.get("plan")
    days = config.get("training_days")
    if not isinstance(plan, dict) or not isinstance(days, dict):
        raise PlannerConfigError("Config needs 'plan' and 'training_days' mappings.")

    for key in (
            "horizon_days",
            "plan_length_weeks",
            "base_weekly_minutes",
            "min_weekly_minutes",
            "max_weekly_minutes",
    ):
        if int(plan.get(key, 0)) <= 0:
            raise PlannerConfigError(f"plan.{key} must be greater than zero.")

    if int(plan["min_weekly_minutes"]) > int(plan["max_weekly_minutes"]):
        raise PlannerConfigError("min_weekly_minutes cannot exceed max_weekly_minutes.")

    if int(plan.get("publish_weeks", 0)) <= 0:
        raise PlannerConfigError("plan.publish_weeks must be greater than zero.")
    publish_weekday = str(plan.get("publish_weekday", "sunday")).strip().lower()
    if publish_weekday not in set(DAY_INDEX) | {"daily", "everyday"}:
        raise PlannerConfigError(
            "plan.publish_weekday must be a weekday name, 'daily', or 'everyday'."
        )

    session_count = 0
    for day_name, slots in days.items():
        if day_name.lower() not in DAY_INDEX:
            raise PlannerConfigError(f"Unknown weekday: {day_name}")
        if slots is None:
            continue
        if not isinstance(slots, list):
            raise PlannerConfigError(f"training_days.{day_name} must be a list.")
        max_per_day = int(plan.get("max_sessions_per_day", 2))
        if len(slots) > max_per_day:
            raise PlannerConfigError(
                f"{day_name} has {len(slots)} sessions, above max_sessions_per_day={max_per_day}."
            )
        primary_count = sum(
            bool(slot.get("primary", index == 0)) for index, slot in enumerate(slots) if isinstance(slot, dict))
        if slots and primary_count == 0:
            raise PlannerConfigError(f"{day_name} needs at least one primary session.")
        for slot in slots:
            session_count += 1
            if not isinstance(slot, dict):
                raise PlannerConfigError(f"A slot on {day_name} is not a mapping.")
            if slot.get("sport") not in SPORT_TYPE:
                raise PlannerConfigError(
                    f"Unsupported sport on {day_name}: {slot.get('sport')}. "
                    "Use run, bike, swim, or strength."
                )
            fixed_minutes = slot.get("fixed_minutes")
            if fixed_minutes is not None and int(fixed_minutes) <= 0:
                raise PlannerConfigError(f"fixed_minutes on {day_name} must be positive.")
            if fixed_minutes is None and float(slot.get("weight", 0)) <= 0:
                raise PlannerConfigError(
                    f"Slot weight on {day_name} must be positive when fixed_minutes is not set."
                )
            if int(slot.get("min_minutes", 0)) <= 0:
                raise PlannerConfigError(f"min_minutes on {day_name} must be positive.")
            if int(slot.get("max_minutes", 0)) < int(slot["min_minutes"]):
                raise PlannerConfigError(
                    f"max_minutes on {day_name} cannot be below min_minutes."
                )
            raw_time = slot.get("time")
            if raw_time is not None:
                try:
                    datetime.strptime(str(raw_time), "%H:%M")
                except ValueError as exc:
                    raise PlannerConfigError(
                        f"time on {day_name} must use 24-hour HH:MM format."
                    ) from exc
            if slot.get("minimum_gap_hours") is not None and float(slot["minimum_gap_hours"]) < 0:
                raise PlannerConfigError(f"minimum_gap_hours on {day_name} cannot be negative.")
            if slot.get("max_gap_minutes") is not None and int(slot["max_gap_minutes"]) < 0:
                raise PlannerConfigError(f"max_gap_minutes on {day_name} cannot be negative.")
    strength_templates = config.get("strength_templates") or {}
    if not isinstance(strength_templates, dict):
        raise PlannerConfigError("strength_templates must be a mapping.")
    strength_count = 0
    for day_name, slots in days.items():
        for slot in slots or []:
            if slot.get("sport") != "strength":
                continue
            strength_count += 1
            template_name = str(slot.get("template", "")).strip()
            if not template_name or template_name not in strength_templates:
                raise PlannerConfigError(
                    f"Strength slot on {day_name} needs a known template name."
                )
    max_strength = int(plan.get("max_strength_sessions_per_week", 2))
    if strength_count > max_strength:
        raise PlannerConfigError(
            f"Configured {strength_count} strength sessions, above "
            f"max_strength_sessions_per_week={max_strength}."
        )
    measurement = config.get("measurement") or {}
    if not isinstance(measurement, dict):
        raise PlannerConfigError("measurement must be a mapping.")
    benchmarks = measurement.get("benchmarks") or {}
    if benchmarks and not isinstance(benchmarks, dict):
        raise PlannerConfigError("measurement.benchmarks must be a mapping.")
    if benchmarks:
        interval = int(benchmarks.get("interval_weeks", 8))
        if interval < 4:
            raise PlannerConfigError("measurement.benchmarks.interval_weeks must be at least 4.")
        sports = benchmarks.get("sports") or {}
        if not isinstance(sports, dict):
            raise PlannerConfigError("measurement.benchmarks.sports must be a mapping.")
        for sport, item in sports.items():
            if sport not in ENDURANCE_SPORTS or not isinstance(item, dict):
                raise PlannerConfigError(
                    "Benchmark sports must be run, bike, or swim mappings."
                )
            preferred = item.get("preferred_weekday")
            if preferred and str(preferred).lower() not in DAY_INDEX:
                raise PlannerConfigError(
                    f"Unknown benchmark preferred weekday: {preferred}"
                )

    if session_count < 3:
        raise PlannerConfigError("Configure at least three weekly sessions.")


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def round_to(value: float, increment: int) -> int:
    return int(round(value / increment) * increment)


def phase_for_week(week_index: int, plan_length_weeks: int) -> str:
    # These proportions keep the full year general rather than race-specific.
    ratio = week_index / max(1, plan_length_weeks)
    if ratio < 0.23:
        return "foundation"
    if ratio < 0.54:
        return "aerobic_build"
    if ratio < 0.77:
        return "vo2_economy"
    return "durability"


def get_nested(obj: Any, *path: str) -> Any:
    current = obj
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def activity_sport(activity: dict[str, Any]) -> str | None:
    raw = (
            get_nested(activity, "activityType", "typeKey")
            or get_nested(activity, "activityType", "typeKey")
            or activity.get("activityTypeKey")
            or activity.get("sportType")
            or ""
    )
    key = str(raw).lower()
    if "run" in key or "treadmill" in key or "track" in key:
        return "run"
    if "cycl" in key or "bike" in key or "biking" in key:
        return "bike"
    if "swim" in key:
        return "swim"
    if "strength" in key or "weight" in key or "resistance" in key:
        return "strength"
    return None


def activity_duration_minutes(activity: dict[str, Any]) -> int:
    seconds = number(
        activity.get("movingDuration")
        or activity.get("duration")
        or activity.get("elapsedDuration")
    )
    return max(0, int(round((seconds or 0) / 60)))


def activity_day(activity: dict[str, Any]) -> date | None:
    raw = (
            activity.get("startTimeLocal")
            or activity.get("startTimeGMT")
            or activity.get("calendarDate")
    )
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(str(raw)[:10])
        except ValueError:
            return None


def fetch_recent_load(client: Garmin, today: date, days: int) -> RecentLoad:
    start = today - timedelta(days=days - 1)
    try:
        activities = client.get_activities_by_date(
            start.isoformat(), today.isoformat(), sortorder="asc"
        )
    except Exception as exc:
        LOG.warning("Could not read recent Garmin activities: %s", exc)
        return RecentLoad(days, 0, None, 0, {"run": 0, "bike": 0, "swim": 0, "strength": 0})

    by_sport = {"run": 0, "bike": 0, "swim": 0, "strength": 0}
    weekly: dict[str, int] = {}
    for activity in activities:
        if not isinstance(activity, dict):
            continue
        sport = activity_sport(activity)
        duration = activity_duration_minutes(activity)
        when = activity_day(activity)
        if sport is None or duration <= 0 or when is None:
            continue
        by_sport[sport] += duration
        # Strength is tracked, but it must not inflate the endurance-volume
        # baseline used to size swim/bike/run work.
        if sport in ENDURANCE_SPORTS:
            week_key = monday_of(when).isoformat()
            weekly[week_key] = weekly.get(week_key, 0) + duration

    active_values = [value for value in weekly.values() if value >= 30]
    weekly_average = (
        int(round(sum(active_values) / len(active_values)))
        if len(active_values) >= 2
        else None
    )
    return RecentLoad(
        days=days,
        total_minutes=sum(by_sport.values()),
        weekly_average_minutes=weekly_average,
        active_weeks=len(active_values),
        by_sport_minutes=by_sport,
    )


def effective_base_minutes(config: dict[str, Any], recent: RecentLoad) -> int:
    plan = config["plan"]
    configured = int(plan["base_weekly_minutes"])
    minimum = int(plan["min_weekly_minutes"])
    maximum = int(plan["max_weekly_minutes"])
    if not bool(plan.get("use_recent_garmin_load", True)):
        return int(clamp(configured, minimum, maximum))
    if recent.weekly_average_minutes is None or recent.active_weeks < 2:
        return int(clamp(configured, minimum, maximum))

    actual = recent.weekly_average_minutes
    if actual < configured:
        return int(clamp(actual * 1.08, minimum, configured))
    return int(clamp(actual * 1.03, configured, maximum))


def weekly_target_minutes(
        config: dict[str, Any], week_index: int, base_minutes: int
) -> int:
    plan = config["plan"]
    growth = float(plan.get("block_growth", 0.04))
    recovery_factor = float(plan.get("recovery_week_factor", 0.72))
    maximum = int(plan["max_weekly_minutes"])
    block = max(0, week_index // 4)
    week_in_block = max(0, week_index % 4)

    coach_multiplier = clamp(float(plan.get("coach_volume_multiplier", 1.0)), 0.85, 1.15)
    block_base = min(maximum, base_minutes * (1 + block * growth))
    multipliers = (0.92, 0.98, 1.04, recovery_factor)
    return round_to(
        min(maximum, block_base * multipliers[week_in_block] * coach_multiplier),
        5,
    )


def slot_duration(
        slot: dict[str, Any], share_minutes: float, multiplier: float = 1.0
) -> int:
    requested = (
        float(slot["fixed_minutes"]) * multiplier
        if slot.get("fixed_minutes") is not None
        else share_minutes
    )
    value = clamp(
        requested,
        int(slot["min_minutes"]),
        int(slot["max_minutes"]),
    )
    return max(5, round_to(value, 5))


def role_intensity(role: str) -> str:
    if role in {"quality", "strength_power"}:
        return "hard"
    if role == "long":
        return "long"
    if role in {"endurance", "strength"}:
        return "moderate"
    return "easy"


def session_title(
        prefix: str,
        week_index: int,
        sport: str,
        role: str,
        phase: str,
        duration: int,
) -> str:
    sport_title = {"run": "Run", "bike": "Bike", "swim": "Swim", "strength": "Strength"}[sport]
    role_title = {
        "technique": "Technique",
        "easy": "Easy",
        "endurance": "Endurance",
        "quality": "Quality",
        "long": "Long",
        "brick": "Brick",
        "recovery": "Recovery",
        "strength": "Full Body",
        "strength_maintenance": "Maintenance",
        "strength_power": "Power",
    }.get(role, role.title())
    phase_tag = {
        "foundation": "Foundation",
        "aerobic_build": "Aerobic",
        "vo2_economy": "VO2",
        "durability": "Durability",
    }[phase]
    return f"[{prefix}] W{week_index + 1:02d} {sport_title} {role_title} {duration}m {phase_tag}"[:80]


def _strength_template_description(
        template_name: str | None,
        exercises: tuple[dict[str, Any], ...],
        config: dict[str, Any],
) -> str:
    template = (config.get("strength_templates") or {}).get(template_name or "", {})
    rounds = int(template.get("rounds", 3))
    target_rpe = template.get("target_rpe", 7)
    rest_seconds = int(template.get("rest_seconds", 90))
    items = []
    for exercise in exercises:
        name = str(exercise.get("name", "Exercise"))
        reps = str(exercise.get("reps", "controlled reps"))
        items.append(f"{name}: {rounds} x {reps}")
    exercise_text = "; ".join(items)
    return (
        f"Strength template {template_name or 'full body'}. {exercise_text}. "
        f"Target RPE {target_rpe}/10, keep about 2-3 repetitions in reserve, "
        f"and rest about {rest_seconds} seconds between demanding sets. "
        "Use controlled technique; never train through sharp pain."
    )


def session_description(
        sport: str,
        role: str,
        phase: str,
        duration: int,
        week_index: int,
        *,
        config: dict[str, Any] | None = None,
        template_name: str | None = None,
        exercises: tuple[dict[str, Any], ...] = (),
) -> str:
    if sport == "strength":
        purpose = _strength_template_description(
            template_name, exercises, config or DEFAULT_CONFIG
        )
    else:
        purpose = {
            "technique": "Keep this technically clean and relaxed. Stop before form deteriorates.",
            "easy": "Conversational effort, RPE 2-4. The purpose is aerobic volume, not proving fitness.",
            "endurance": "Steady aerobic work, mostly RPE 3-5 with smooth technique.",
            "quality": "Controlled quality. Hard repetitions are RPE 8-9; recoveries must be genuinely easy.",
            "long": "Easy durable aerobic work. Fuel and hydrate; avoid turning the finish into a race.",
            "brick": "Very easy transition run. Short stride and relaxed effort after the bike.",
            "recovery": "Recovery only, RPE 1-2. Stop if symptoms, pain, or unusual fatigue appear.",
        }.get(role, "Complete the session at a controlled effort.")
    return (
        f"Adaptive year-base plan, week {week_index + 1}, phase {phase}. "
        f"Planned duration about {duration} minutes. {purpose} "
        "This workout is managed by the self-hosted Garmin planner. "
        "After completing it, optionally add this to the activity's private description: "
        "#coach soreness=[0-10] fatigue=[0-10] motivation=[0-10] "
        "pain=[yes/no] rpe=[1-10]"
    )


def build_week_sessions(
        config: dict[str, Any],
        week_start: date,
        plan_start: date,
        base_minutes: int,
) -> list[Session]:
    week_index = max(0, (week_start - monday_of(plan_start)).days // 7)
    plan = config["plan"]
    phase = phase_for_week(week_index, int(plan["plan_length_weeks"]))
    target = weekly_target_minutes(config, week_index, base_minutes)
    prefix = str(plan.get("name_prefix", "AI BASE")).strip() or "AI BASE"

    flattened: list[tuple[str, int, dict[str, Any]]] = []
    for day_name, slots in config["training_days"].items():
        for slot_index, slot in enumerate(slots or []):
            flattened.append((day_name.lower(), slot_index, slot))

    sport_multipliers = plan.get("sport_weight_multipliers") or {}
    adjusted_weights = [
        (
            float(slot.get("weight", 1.0))
            * float(sport_multipliers.get(str(slot["sport"]), 1.0))
            if str(slot["sport"]) in ENDURANCE_SPORTS
               and slot.get("fixed_minutes") is None
            else 0.0
        )
        for _, _, slot in flattened
    ]
    total_weight = sum(adjusted_weights)
    if total_weight <= 0:
        raise PlannerConfigError("At least one endurance session needs a positive weight.")
    sessions: list[Session] = []
    hard_count = 0
    hard_by_day: dict[str, int] = {}
    max_hard = int(plan.get("max_hard_sessions_per_week", 2))
    max_hard_per_day = int(plan.get("max_hard_sessions_per_day", 1))
    recovery_week = week_index % 4 == 3

    for (day_name, slot_index, slot), adjusted_weight in zip(flattened, adjusted_weights):
        sport = str(slot["sport"])
        role = str(slot.get("role", "easy"))
        share = target * adjusted_weight / total_weight if adjusted_weight > 0 else 0
        session_multiplier = float(sport_multipliers.get(sport, 1.0))
        if sport == "strength":
            session_multiplier *= clamp(
                float(plan.get("coach_volume_multiplier", 1.0)), 0.85, 1.15
            )
            if recovery_week:
                session_multiplier *= float(plan.get("recovery_week_factor", 0.72))
        duration = slot_duration(slot, share, session_multiplier)

        # Recovery weeks retain frequency but remove systemic intensity.
        if recovery_week and role == "quality":
            role = "easy" if sport != "swim" else "technique"
        elif recovery_week and sport == "strength":
            role = "strength_maintenance"

        intensity = role_intensity(role)
        if intensity == "hard":
            hard_count += 1
            hard_by_day[day_name] = hard_by_day.get(day_name, 0) + 1
            if hard_count > max_hard or hard_by_day[day_name] > max_hard_per_day:
                role = "easy" if sport != "swim" else "technique"
                intensity = "easy"

        day = week_start + timedelta(days=DAY_INDEX[day_name])
        key = f"{day.isoformat()}:{sport}:{role}:{slot_index}"
        template_name = str(slot.get("template")) if slot.get("template") else None
        template = (config.get("strength_templates") or {}).get(template_name or "", {})
        strength_exercises = tuple(
            item for item in (template.get("exercises") or []) if isinstance(item, dict)
        )
        sessions.append(
            Session(
                key=key,
                day=day.isoformat(),
                sport=sport,
                role=role,
                duration_minutes=duration,
                name=session_title(
                    prefix, week_index, sport, role, phase, duration
                ),
                description=session_description(
                    sport,
                    role,
                    phase,
                    duration,
                    week_index,
                    config=config,
                    template_name=template_name,
                    exercises=strength_exercises,
                ),
                intensity=intensity,
                priority=int(slot.get("priority", 50)),
                phase=phase,
                week_index=week_index,
                slot_index=slot_index,
                scheduled_time=str(slot.get("time")) if slot.get("time") else None,
                primary=bool(slot.get("primary", slot_index == 0)),
                after=str(slot.get("after")) if slot.get("after") else None,
                minimum_gap_hours=(
                    float(slot["minimum_gap_hours"])
                    if slot.get("minimum_gap_hours") is not None
                    else None
                ),
                max_gap_minutes=(
                    int(slot["max_gap_minutes"])
                    if slot.get("max_gap_minutes") is not None
                    else None
                ),
                strength_template=template_name,
                strength_exercises=strength_exercises,
            )
        )
    benchmark_sessions = apply_due_benchmark(
        config,
        sessions,
        week_index=week_index,
        recovery_week=recovery_week,
    )
    return annotate_sessions(config, benchmark_sessions)


def build_sessions_for_range(
        config: dict[str, Any],
        start_day: date,
        end_day: date,
        plan_start: date,
        base_minutes: int,
) -> list[Session]:
    sessions: list[Session] = []
    week = monday_of(start_day)
    while week <= end_day:
        for session in build_week_sessions(config, week, plan_start, base_minutes):
            when = date.fromisoformat(session.day)
            if start_day <= when <= end_day:
                sessions.append(session)
        week += timedelta(days=7)
    return sorted(sessions, key=lambda item: (item.day, item.slot_index, item.sport))


def no_target() -> dict[str, Any]:
    return {
        "workoutTargetTypeId": TargetType.NO_TARGET,
        "workoutTargetTypeKey": "no.target",
        "displayOrder": 1,
    }


def distance_step(
        meters: float,
        step_order: int,
        step_type: int = StepType.INTERVAL,
        step_key: str = "interval",
) -> ExecutableStep:
    return ExecutableStep(
        stepOrder=step_order,
        stepType={
            "stepTypeId": step_type,
            "stepTypeKey": step_key,
            "displayOrder": step_type,
        },
        endCondition={
            "conditionTypeId": ConditionType.DISTANCE,
            "conditionTypeKey": "distance",
            "displayOrder": 3,
            "displayable": True,
        },
        endConditionValue=float(meters),
        targetType=no_target(),
    )


def rest_step(seconds: float, step_order: int) -> ExecutableStep:
    return ExecutableStep(
        stepOrder=step_order,
        stepType={
            "stepTypeId": StepType.REST,
            "stepTypeKey": "rest",
            "displayOrder": 5,
        },
        endCondition={
            "conditionTypeId": ConditionType.FIXED_REST,
            "conditionTypeKey": "fixed.rest",
            "displayOrder": 8,
            "displayable": True,
        },
        endConditionValue=float(seconds),
        targetType=no_target(),
    )


def session_target(session: Session) -> dict[str, Any] | None:
    if (
            not session.target_kind
            or session.target_kind == "description_only"
            or session.target_low is None
            or session.target_high is None
    ):
        return None
    mapping = {
        "heart_rate": (
            TargetType.HEART_RATE_ZONE,
            "heart.rate.zone",
            4,
        ),
        "pace": (TargetType.PACE_ZONE, "pace.zone", 6),
        "power": (TargetType.POWER_ZONE, "power.zone", 2),
    }
    if session.target_kind not in mapping:
        return None
    target_id, target_key, display_order = mapping[session.target_kind]
    return {
        "targetType": {
            "workoutTargetTypeId": target_id,
            "workoutTargetTypeKey": target_key,
            "displayOrder": display_order,
        },
        "targetValueOne": float(min(session.target_low, session.target_high)),
        "targetValueTwo": float(max(session.target_low, session.target_high)),
    }


def time_step(
        duration_seconds: float,
        step_order: int,
        step_type: int,
        step_key: str,
        target: dict[str, Any] | None = None,
) -> ExecutableStep:
    kwargs: dict[str, Any] = {
        "stepOrder": step_order,
        "stepType": {
            "stepTypeId": step_type,
            "stepTypeKey": step_key,
            "displayOrder": step_type,
        },
        "endCondition": {
            "conditionTypeId": ConditionType.TIME,
            "conditionTypeKey": "time",
            "displayOrder": 2,
            "displayable": True,
        },
        "endConditionValue": float(duration_seconds),
        "targetType": no_target(),
    }
    if target:
        kwargs.update(target)
    return ExecutableStep(**kwargs)


def easy_time_steps(
        minutes: int, target: dict[str, Any] | None = None
) -> list[ExecutableStep | RepeatGroup]:
    total = max(15, minutes * 60)
    warmup = min(600, max(300, total * 0.15))
    cooldown = min(600, max(300, total * 0.12))
    main = max(300, total - warmup - cooldown)
    return [
        create_warmup_step(float(warmup), step_order=1),
        time_step(float(main), 2, StepType.INTERVAL, "interval", target),
        create_cooldown_step(float(cooldown), step_order=3),
    ]


def quality_time_steps(
        sport: str,
        phase: str,
        minutes: int,
        progression_level: int = 0,
        target: dict[str, Any] | None = None,
) -> list[ExecutableStep | RepeatGroup]:
    total = max(40, minutes) * 60
    warmup = 12 * 60 if sport == "run" else 15 * 60
    cooldown = 10 * 60

    if phase == "foundation":
        reps, hard, recovery = (6, 60, 90) if sport == "run" else (6, 120, 120)
    elif phase == "aerobic_build":
        reps, hard, recovery = (3, 8 * 60, 3 * 60) if sport == "run" else (4, 8 * 60, 4 * 60)
    elif phase == "vo2_economy":
        reps, hard, recovery = (5, 3 * 60, 3 * 60) if sport == "run" else (5, 4 * 60, 4 * 60)
    else:
        reps, hard, recovery = (2, 15 * 60, 5 * 60) if sport == "run" else (3, 12 * 60, 5 * 60)

    progression_level = max(-2, min(3, int(progression_level)))
    reps = max(2, reps + progression_level)
    interval_block = reps * (hard + recovery)
    if warmup + cooldown + interval_block > total:
        reps = max(2, int((total - warmup - cooldown) // (hard + recovery)))
    hard_step = time_step(float(hard), 1, StepType.INTERVAL, "interval", target)
    recover_step = create_recovery_step(float(recovery), step_order=2)
    repeat = create_repeat_group(reps, [hard_step, recover_step], step_order=2)
    return [
        create_warmup_step(float(warmup), step_order=1),
        repeat,
        create_cooldown_step(float(cooldown), step_order=3),
    ]


def swim_total_meters(config: dict[str, Any], minutes: int) -> int:
    pool = int(config["plan"].get("pool_length_m", 25))
    per_hour = int(config["plan"].get("swim_meters_per_hour", 2200))
    estimate = minutes * per_hour / 60
    return max(pool * 16, round_to(estimate, pool))


def swim_steps(
        config: dict[str, Any],
        role: str,
        phase: str,
        minutes: int,
        progression_level: int = 0,
) -> list[ExecutableStep | RepeatGroup]:
    pool = int(config["plan"].get("pool_length_m", 25))
    total = swim_total_meters(config, minutes)
    warmup = max(200, pool * 8)
    cooldown = max(100, pool * 4)
    remaining = max(pool * 8, total - warmup - cooldown)

    steps: list[ExecutableStep | RepeatGroup] = [
        distance_step(warmup, 1, StepType.WARMUP, "warmup")
    ]

    if role in {"technique", "recovery", "easy"}:
        drill_distance = max(50, pool * 2)
        repetitions = max(4, min(12, remaining // drill_distance))
        group = create_repeat_group(
            repetitions,
            [distance_step(drill_distance, 1), rest_step(20, 2)],
            step_order=2,
        )
        steps.append(group)
    elif role == "quality":
        repeat_distance = 100 if pool <= 25 else 200
        repetitions = max(
            4,
            min(14, remaining // repeat_distance + max(-2, min(3, progression_level))),
        )
        rest = 25 if phase != "vo2_economy" else 35
        steps.append(
            create_repeat_group(
                repetitions,
                [distance_step(repeat_distance, 1), rest_step(rest, 2)],
                step_order=2,
            )
        )
    else:
        repeat_distance = 200 if remaining < 2000 else 400
        repetitions = max(2, min(10, remaining // repeat_distance))
        steps.append(
            create_repeat_group(
                repetitions,
                [distance_step(repeat_distance, 1), rest_step(25, 2)],
                step_order=2,
            )
        )

    steps.append(distance_step(cooldown, 3, StepType.COOLDOWN, "cooldown"))
    return steps


def benchmark_time_steps(session: Session) -> list[ExecutableStep | RepeatGroup]:
    protocol = session.benchmark_id
    if protocol == "run_aerobic_30":
        return [
            create_warmup_step(10 * 60.0, step_order=1),
            create_interval_step(30 * 60.0, step_order=2),
            create_cooldown_step(10 * 60.0, step_order=3),
        ]
    if protocol == "run_threshold_20":
        strides = create_repeat_group(
            4,
            [
                create_interval_step(20.0, step_order=1),
                create_recovery_step(40.0, step_order=2),
            ],
            step_order=2,
        )
        return [
            create_warmup_step(15 * 60.0, step_order=1),
            strides,
            create_recovery_step(3 * 60.0, step_order=3),
            create_interval_step(20 * 60.0, step_order=4),
            create_cooldown_step(10 * 60.0, step_order=5),
        ]
    if protocol == "bike_aerobic_45":
        return [
            create_warmup_step(15 * 60.0, step_order=1),
            create_interval_step(45 * 60.0, step_order=2),
            create_cooldown_step(10 * 60.0, step_order=3),
        ]
    if protocol == "bike_threshold_20":
        return [
            create_warmup_step(20 * 60.0, step_order=1),
            create_interval_step(3 * 60.0, step_order=2),
            create_recovery_step(5 * 60.0, step_order=3),
            create_interval_step(20 * 60.0, step_order=4),
            create_cooldown_step(10 * 60.0, step_order=5),
        ]
    return easy_time_steps(session.duration_minutes)


def benchmark_swim_steps(
        config: dict[str, Any], session: Session
) -> list[ExecutableStep | RepeatGroup]:
    pool = int(config["plan"].get("pool_length_m", 25))
    long_distance = max(pool * 8, round(400 / pool) * pool)
    short_distance = max(pool * 4, round(200 / pool) * pool)
    warmup = max(pool * 8, round(300 / pool) * pool)
    cooldown = max(pool * 4, round(200 / pool) * pool)
    return [
        distance_step(warmup, 1, StepType.WARMUP, "warmup"),
        create_repeat_group(
            4,
            [distance_step(pool * 2, 1), rest_step(20, 2)],
            step_order=2,
        ),
        distance_step(long_distance, 3, StepType.INTERVAL, "long benchmark effort"),
        rest_step(5 * 60, 4),
        distance_step(short_distance, 5, StepType.INTERVAL, "short benchmark effort"),
        distance_step(cooldown, 6, StepType.COOLDOWN, "cooldown"),
    ]


def workout_for_session(config: dict[str, Any], session: Session) -> Any:
    quality_progression = config.get("plan", {}).get("quality_progression") or {}
    progression_level = int(quality_progression.get(session.sport, 0))
    target = session_target(session)
    if session.benchmark_id and session.sport == "swim":
        steps = benchmark_swim_steps(config, session)
    elif session.benchmark_id:
        steps = benchmark_time_steps(session)
    elif session.sport == "strength":
        steps = easy_time_steps(session.duration_minutes)
    elif session.sport == "swim":
        steps = swim_steps(
            config,
            session.role,
            session.phase,
            session.duration_minutes,
            progression_level,
        )
    elif session.role == "quality":
        steps = quality_time_steps(
            session.sport,
            session.phase,
            session.duration_minutes,
            progression_level,
            target,
        )
    else:
        steps = easy_time_steps(session.duration_minutes, target)

    segment = WorkoutSegment(
        segmentOrder=1,
        sportType=SPORT_TYPE[session.sport],
        workoutSteps=steps,
    )
    kwargs = {
        "workoutName": session.name,
        "estimatedDurationInSecs": session.duration_minutes * 60,
        "workoutSegments": [segment],
        "description": (
                session.description
                + (f" Suggested start time: {session.scheduled_time}." if session.scheduled_time else "")
                + (f" Complete after: {session.after}." if session.after else "")
        ),
    }
    if session.sport == "run":
        return RunningWorkout(**kwargs)
    if session.sport == "bike":
        return CyclingWorkout(**kwargs)
    if session.sport == "swim":
        return SwimmingWorkout(**kwargs)
    return BaseWorkout(sportType=SPORT_TYPE["strength"], **kwargs)


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
            nested = first_positive_int(value, wanted)
            if nested is not None:
                return nested
    elif isinstance(obj, list):
        for item in obj:
            nested = first_positive_int(item, wanted)
            if nested is not None:
                return nested
    return None


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 2, "sessions": {}, "daily_adaptations": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read planner state {path}: {exc}") from exc
    state.setdefault("version", 2)
    state.setdefault("sessions", {})
    state.setdefault("daily_adaptations", {})
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now().astimezone().isoformat()
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    temp.replace(path)
    try:
        from training_db import sync_planner_state

        sync_planner_state(path.parent, state)
    except Exception as exc:
        LOG.warning("Could not sync planner state to the measurement database: %s", exc)


class CalendarCache:
    def __init__(self, client: Garmin):
        self.client = client
        self.months: dict[tuple[int, int], Any] = {}

    def payload(self, day: date) -> Any:
        key = (day.year, day.month)
        if key not in self.months:
            self.months[key] = self.client.get_scheduled_workouts(day.year, day.month)
        return self.months[key]

    def entries(self, day: date) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                text_values = [str(v) for v in node.values() if isinstance(v, str)]
                if any(day.isoformat() in value for value in text_values) and any(
                        key in node
                        for key in (
                                "workoutId",
                                "scheduledWorkoutId",
                                "workoutScheduleId",
                                "workoutName",
                                "title",
                        )
                ):
                    found.append(node)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(self.payload(day))
        return found

    def find(
            self, day: date, workout_id: int | None = None, name: str | None = None
    ) -> dict[str, Any] | None:
        for entry in self.entries(day):
            entry_workout_id = first_positive_int(entry, ("workoutId",))
            label = str(
                entry.get("workoutName")
                or entry.get("title")
                or entry.get("name")
                or ""
            )
            if workout_id and entry_workout_id == workout_id:
                return entry
            if name and label == name:
                return entry
        return None

    def invalidate(self, day: date) -> None:
        self.months.pop((day.year, day.month), None)


class AdaptivePlanner:
    def __init__(
            self,
            client: Garmin,
            config: dict[str, Any],
            data_dir: Path,
            write_enabled: bool,
            dry_run: bool,
    ) -> None:
        self.client = client
        self.data_dir = data_dir
        self.config = apply_adjustment_to_config(config, data_dir)
        self.config["_resolved_targets"] = resolve_training_targets(
            self.config, data_dir
        )
        self.state_path = data_dir / "planner_state.json"
        self.state = load_state(self.state_path)
        self.write_enabled = write_enabled
        self.dry_run = dry_run
        self.can_write = write_enabled and not dry_run
        self.calendar = CalendarCache(client)

    def _plan_start(self, today: date) -> date:
        raw = self.state.get("plan_started_on")
        if raw:
            return date.fromisoformat(raw)
        configured = self.config["plan"].get("start_date")
        start = date.fromisoformat(str(configured)) if configured else today
        self.state["plan_started_on"] = start.isoformat()
        return start

    def _session_record(self, session: Session) -> dict[str, Any]:
        return {
            "session": asdict(session),
            "fingerprint": session.fingerprint(),
            "status": "planned",
            "workout_id": None,
            "scheduled_workout_id": None,
            "created_at": datetime.now().astimezone().isoformat(),
        }

    def _upload_and_schedule(
            self, session: Session, record: dict[str, Any]
    ) -> dict[str, Any]:
        action = {
            "action": "create_and_schedule",
            "session_key": session.key,
            "date": session.day,
            "name": session.name,
            "sport": session.sport,
            "duration_minutes": session.duration_minutes,
            "target": session.target_summary,
            "target_source": session.target_source,
            "write_enabled": self.write_enabled,
            "dry_run": self.dry_run,
            "status": "preview",
        }
        if not self.can_write:
            return action

        workout = workout_for_session(self.config, session)
        if session.sport == "run":
            upload_response = self.client.upload_running_workout(workout)
        elif session.sport == "bike":
            upload_response = self.client.upload_cycling_workout(workout)
        elif session.sport == "swim":
            upload_response = self.client.upload_swimming_workout(workout)
        else:
            # python-garminconnect exposes a generic upload_workout endpoint and
            # Garmin's workout type table defines strength_training as sport 5.
            upload_response = self.client.upload_workout(workout.to_dict())

        workout_id = first_positive_int(upload_response, ("workoutId",))
        if workout_id is None and isinstance(upload_response, dict):
            try:
                fallback_id = int(upload_response.get("id", 0))
            except (TypeError, ValueError):
                fallback_id = 0
            workout_id = fallback_id if fallback_id > 0 else None
        if workout_id is None:
            raise RuntimeError(
                f"Garmin uploaded '{session.name}' but did not return a workout ID."
            )
        schedule_response = self.client.schedule_workout(workout_id, session.day)
        scheduled_id = first_positive_int(
            schedule_response,
            ("scheduledWorkoutId", "workoutScheduleId", "scheduleId"),
        )
        self.calendar.invalidate(date.fromisoformat(session.day))
        if scheduled_id is None:
            entry = self.calendar.find(
                date.fromisoformat(session.day), workout_id=workout_id, name=session.name
            )
            scheduled_id = first_positive_int(
                entry,
                ("scheduledWorkoutId", "workoutScheduleId", "scheduleId"),
            )

        record.update(
            {
                "status": "scheduled",
                "workout_id": workout_id,
                "scheduled_workout_id": scheduled_id,
                "upload_response": upload_response,
                "schedule_response": schedule_response,
            }
        )
        action.update(
            {
                "status": "applied",
                "workout_id": workout_id,
                "scheduled_workout_id": scheduled_id,
            }
        )
        return action

    def _find_scheduled_id(self, record: dict[str, Any]) -> int | None:
        scheduled = first_positive_int(
            record,
            ("scheduled_workout_id", "scheduledWorkoutId", "workoutScheduleId"),
        )
        if scheduled:
            return scheduled
        raw_session = record.get("session", {})
        if not raw_session.get("day"):
            return None
        entry = self.calendar.find(
            date.fromisoformat(raw_session["day"]),
            workout_id=first_positive_int(record, ("workout_id", "workoutId")),
            name=raw_session.get("name"),
        )
        return first_positive_int(
            entry,
            ("scheduledWorkoutId", "workoutScheduleId", "scheduleId"),
        )

    def _unschedule_record(self, record: dict[str, Any], reason: str) -> dict[str, Any]:
        session = record.get("session", {})
        action = {
            "action": "unschedule",
            "session_key": session.get("key"),
            "date": session.get("day"),
            "name": session.get("name"),
            "reason": reason,
            "write_enabled": self.write_enabled,
            "dry_run": self.dry_run,
            "status": "preview",
        }
        scheduled_id = self._find_scheduled_id(record)
        if self.can_write and scheduled_id:
            try:
                self.client.unschedule_workout(scheduled_id)
            except Exception as exc:
                if not is_missing_workout_schedule_error(exc):
                    raise

                # The stored ID can be stale after a manual calendar edit or an
                # interrupted rebuild. Refresh the day's calendar and remove a
                # matching live entry when Garmin assigned it a different ID.
                refreshed_id: int | None = None
                raw_day = session.get("day")
                if raw_day:
                    session_day = date.fromisoformat(raw_day)
                    self.calendar.invalidate(session_day)
                    entry = self.calendar.find(
                        session_day,
                        workout_id=first_positive_int(
                            record, ("workout_id", "workoutId")
                        ),
                        name=session.get("name"),
                    )
                    refreshed_id = first_positive_int(
                        entry,
                        (
                            "scheduledWorkoutId",
                            "workoutScheduleId",
                            "scheduleId",
                        ),
                    )

                if refreshed_id and refreshed_id != scheduled_id:
                    try:
                        self.client.unschedule_workout(refreshed_id)
                    except Exception as refreshed_exc:
                        if not is_missing_workout_schedule_error(refreshed_exc):
                            raise
                        action["status"] = "already_missing"
                        action["note"] = (
                            "The saved and refreshed Garmin schedule entries "
                            "were already absent."
                        )
                    else:
                        scheduled_id = refreshed_id
                        action["status"] = "applied_refreshed_id"
                        action["note"] = (
                            "The saved Garmin schedule ID was stale; the live "
                            "matching entry was removed using its refreshed ID."
                        )
                else:
                    LOG.warning(
                        "Garmin schedule %s for '%s' is already absent; "
                        "continuing %s.",
                        scheduled_id,
                        session.get("name") or session.get("key"),
                        reason,
                    )
                    action["status"] = "already_missing"
                    action["note"] = (
                        "Garmin returned 404 because this schedule entry had "
                        "already been removed."
                    )
            else:
                action["status"] = "applied"

            if session.get("day"):
                self.calendar.invalidate(date.fromisoformat(session["day"]))
            action["scheduled_workout_id"] = scheduled_id
            record["status"] = "cancelled"
            record["cancel_reason"] = reason
            record["cancelled_at"] = datetime.now().astimezone().isoformat()
        elif self.can_write and not scheduled_id:
            action["status"] = "not_found"
            record["status"] = "cancelled"
            record["cancel_reason"] = reason
            record["cancelled_at"] = datetime.now().astimezone().isoformat()
        return action

    def _publish_is_due(self, today: date) -> bool:
        configured = str(
            self.config["plan"].get("publish_weekday", "sunday")
        ).strip().lower()
        return configured in {"daily", "everyday"} or today.strftime("%A").lower() == configured

    def _has_active_future_sessions(self, today: date) -> bool:
        for record in self.state["sessions"].values():
            session = record.get("session", {})
            raw_day = session.get("day")
            if not raw_day or record.get("status") not in {"planned", "scheduled", "adapted"}:
                continue
            try:
                if date.fromisoformat(raw_day) >= today:
                    return True
            except ValueError:
                continue
        return False

    def _publication_range(
            self, today: date, *, force_publish: bool
    ) -> tuple[date, date, str]:
        weeks = max(1, int(self.config["plan"].get("publish_weeks", 2)))
        if force_publish:
            start = today
            reason = "manual"
        else:
            # The automatic Sunday publication starts on Monday. On each Sunday,
            # the overlapping week is retained and only the newly exposed second
            # week normally needs to be added.
            start = today + timedelta(days=1)
            reason = "scheduled_weekly_publish"
        return start, start + timedelta(days=weeks * 7 - 1), reason

    def _state_sessions_between(self, start: date, end: date) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for record in self.state["sessions"].values():
            session = record.get("session", {})
            raw_day = session.get("day")
            if not raw_day or record.get("status") not in {"planned", "scheduled", "adapted"}:
                continue
            try:
                when = date.fromisoformat(raw_day)
            except ValueError:
                continue
            if start <= when <= end:
                output.append(session)
        return sorted(
            output,
            key=lambda item: (item.get("day", ""), item.get("slot_index", 0)),
        )

    def ensure_horizon(
            self, today: date, *, force_publish: bool = False
    ) -> dict[str, Any]:
        plan = self.config["plan"]
        recent = fetch_recent_load(
            self.client, today, int(plan.get("recent_load_days", 28))
        )
        base = effective_base_minutes(self.config, recent)
        self.state["recent_load"] = asdict(recent)
        self.state["effective_base_weekly_minutes"] = base
        plan_start = self._plan_start(today)

        due = self._publish_is_due(today)
        bootstrap = bool(plan.get("publish_bootstrap_when_empty", True)) and not self._has_active_future_sessions(today)
        if not force_publish and not due and not bootstrap:
            weeks = max(1, int(plan.get("publish_weeks", 2)))
            visible_end = today + timedelta(days=weeks * 7 - 1)
            current = self._state_sessions_between(today, visible_end)
            self._prune_state(today)
            save_state(self.state_path, self.state)
            return {
                "status": "publication_skipped",
                "publication": {
                    "publish_weekday": plan.get("publish_weekday", "sunday"),
                    "publish_weeks": weeks,
                    "next_automatic_publish": "next configured weekday",
                    "reason": "automatic publishing only runs on the configured weekday",
                },
                "range": {"from": today.isoformat(), "to": visible_end.isoformat()},
                "effective_base_weekly_minutes": base,
                "recent_load": asdict(recent),
                "resolved_targets": self.config.get("_resolved_targets", {}),
                "actions": [],
                "planned_sessions": current,
            }

        if force_publish:
            publish_start, publish_end, publish_reason = self._publication_range(
                today, force_publish=True
            )
        elif bootstrap and not due:
            publish_start = today
            weeks = max(1, int(plan.get("publish_weeks", 2)))
            publish_end = today + timedelta(days=weeks * 7 - 1)
            publish_reason = "bootstrap_empty_calendar"
        else:
            publish_start, publish_end, publish_reason = self._publication_range(
                today, force_publish=False
            )

        sessions = build_sessions_for_range(
            self.config, publish_start, publish_end, plan_start, base
        )
        actions: list[dict[str, Any]] = []

        # Remove planner-owned sessions beyond the new two-week window. This is
        # especially important when upgrading from the old 21-day horizon.
        outside_records: list[dict[str, Any]] = []
        for record in self.state["sessions"].values():
            session = record.get("session", {})
            raw_day = session.get("day")
            if not raw_day or record.get("status") not in {"planned", "scheduled", "adapted"}:
                continue
            try:
                when = date.fromisoformat(raw_day)
            except ValueError:
                continue
            if when > publish_end:
                outside_records.append(record)

        if not self.can_write:
            for record in outside_records:
                actions.append(
                    {
                        "action": "would_unschedule_outside_publish_window",
                        "date": record.get("session", {}).get("day"),
                        "name": record.get("session", {}).get("name"),
                        "status": "preview",
                    }
                )
            for session in sessions:
                record = self.state["sessions"].get(session.key)
                fingerprint = session.fingerprint()
                if record and record.get("fingerprint") == fingerprint and record.get(
                        "status"
                ) in {"scheduled", "adapted"}:
                    continue
                if record and record.get("status") in {"scheduled", "adapted"}:
                    actions.append(
                        {
                            "action": "would_replace_changed_session",
                            "session_key": session.key,
                            "date": session.day,
                            "old_name": record.get("session", {}).get("name"),
                            "new_name": session.name,
                            "target": session.target_summary,
                            "status": "preview",
                        }
                    )
                else:
                    actions.append(
                        {
                            "action": "create_and_schedule",
                            "session_key": session.key,
                            "date": session.day,
                            "name": session.name,
                            "sport": session.sport,
                            "duration_minutes": session.duration_minutes,
                            "target": session.target_summary,
                            "write_enabled": self.write_enabled,
                            "dry_run": self.dry_run,
                            "status": "preview",
                        }
                    )
            save_state(self.state_path, self.state)
            return {
                "status": "preview",
                "publication": {
                    "reason": publish_reason,
                    "publish_weekday": plan.get("publish_weekday", "sunday"),
                    "publish_weeks": int(plan.get("publish_weeks", 2)),
                },
                "range": {
                    "from": publish_start.isoformat(),
                    "to": publish_end.isoformat(),
                },
                "effective_base_weekly_minutes": base,
                "recent_load": asdict(recent),
                "resolved_targets": self.config.get("_resolved_targets", {}),
                "actions": actions,
                "planned_sessions": [asdict(item) for item in sessions],
            }

        for record in outside_records:
            actions.append(
                self._unschedule_record(record, "outside weekly publication window")
            )

        for session in sessions:
            record = self.state["sessions"].get(session.key)
            fingerprint = session.fingerprint()
            if record and record.get("fingerprint") == fingerprint:
                if record.get("status") == "planned":
                    actions.append(self._upload_and_schedule(session, record))
                continue

            if record and record.get("status") in {"scheduled", "adapted"}:
                actions.append(
                    self._unschedule_record(record, "plan definition or target changed")
                )

            record = self._session_record(session)
            self.state["sessions"][session.key] = record
            actions.append(self._upload_and_schedule(session, record))

        self.state["last_publication"] = {
            "date": today.isoformat(),
            "range_from": publish_start.isoformat(),
            "range_to": publish_end.isoformat(),
            "reason": publish_reason,
        }
        self._prune_state(today)
        save_state(self.state_path, self.state)
        return {
            "status": "published",
            "publication": self.state["last_publication"],
            "range": {
                "from": publish_start.isoformat(),
                "to": publish_end.isoformat(),
            },
            "effective_base_weekly_minutes": base,
            "recent_load": asdict(recent),
            "resolved_targets": self.config.get("_resolved_targets", {}),
            "actions": actions,
            "planned_sessions": [asdict(item) for item in sessions],
        }

    def _active_records_for_day(self, today: date) -> list[dict[str, Any]]:
        active = []
        for record in self.state["sessions"].values():
            session = record.get("session", {})
            if session.get("day") != today.isoformat():
                continue
            if record.get("status") in {"planned", "scheduled", "adapted"}:
                active.append(record)
        return active

    def _replacement_session(
            self,
            original: Session,
            status: str,
            index: int,
    ) -> Session:
        plan = self.config["plan"]
        if status == "AMBER":
            factor = float(plan.get("amber_duration_factor", 0.72))
            duration = max(20, round_to(original.duration_minutes * factor, 5))
            if original.sport == "swim":
                role = "technique"
            elif original.sport == "strength":
                role = "strength_maintenance"
            else:
                role = "easy"
        else:
            duration = int(plan.get("red_duration_minutes", 30))
            role = "recovery"

        prefix = str(plan.get("name_prefix", "AI BASE")).strip() or "AI BASE"
        key = f"{original.day}:{original.sport}:{role}:adaptive-{index}"
        replacement = replace(
            original,
            key=key,
            role=role,
            duration_minutes=duration,
            name=f"[{prefix}] {status} {original.sport.title()} {role.title()} {duration}m"[:80],
            description=(
                    f"Recovery adaptation from {status} morning signals. "
                    f"Original session: {original.name}. "
                    + session_description(
                original.sport,
                role,
                original.phase,
                duration,
                original.week_index,
                config=self.config,
                template_name=original.strength_template,
                exercises=original.strength_exercises,
            )
            ),
            intensity="easy",
            slot_index=100 + index,
            target_kind=None,
            target_low=None,
            target_high=None,
            target_unit=None,
            target_summary=None,
            target_source=None,
        )
        return annotate_sessions(self.config, [replacement])[0]

    def adapt_today(
            self, today: date, recovery_status: str, reasons: list[str]
    ) -> dict[str, Any]:
        status = recovery_status.upper()
        previous = str(
            self.state["daily_adaptations"].get(today.isoformat(), {}).get(
                "status", "GREEN"
            )
        ).upper()
        if self.can_write and SEVERITY.get(status, 0) <= SEVERITY.get(previous, 0):
            return {
                "status": "already_evaluated",
                "recovery_status": status,
                "previous_status": previous,
                "actions": [],
            }
        if status not in {"AMBER", "RED"}:
            return {"status": "kept", "recovery_status": status, "actions": []}

        records = self._active_records_for_day(today)
        if not records and not self.can_write:
            start = self._plan_start(today)
            base = int(
                self.state.get("effective_base_weekly_minutes")
                or self.config["plan"]["base_weekly_minutes"]
            )
            virtual_sessions = build_sessions_for_range(
                self.config, today, today, start, base
            )
            records = [self._session_record(item) for item in virtual_sessions]

        if not records:
            if self.can_write:
                self.state["daily_adaptations"][today.isoformat()] = {
                    "status": status,
                    "reasons": reasons,
                    "actions": [],
                }
                save_state(self.state_path, self.state)
            return {
                "status": "no_managed_session_today",
                "recovery_status": status,
                "actions": [],
            }

        actions: list[dict[str, Any]] = []
        replacements: list[Session] = []

        if status == "AMBER":
            for index, record in enumerate(records):
                original = Session(**record["session"])
                if original.role == "brick" or (
                        len(records) > 1 and not original.primary
                ):
                    actions.append(
                        self._unschedule_record(record, "AMBER: remove secondary session")
                    )
                    continue
                if original.intensity in {"hard", "long", "moderate"}:
                    actions.append(
                        self._unschedule_record(record, "AMBER: downgrade session")
                    )
                    replacements.append(
                        self._replacement_session(original, status, index)
                    )
        else:
            originals = [Session(**record["session"]) for record in records]
            for record in records:
                actions.append(self._unschedule_record(record, "RED recovery day"))
            red_action = str(
                self.config["plan"].get("red_action", "recovery")
            ).lower()
            if red_action != "rest":
                preferred = next((s for s in originals if s.sport == "swim"), None)
                preferred = preferred or next(
                    (s for s in originals if s.sport == "bike"), None
                )
                preferred = preferred or next(
                    (s for s in originals if s.sport == "run"), None
                )
                # Do not replace an all-strength RED day with more strength.
                if preferred is not None:
                    replacements.append(self._replacement_session(preferred, status, 0))

        for replacement in replacements:
            record = self._session_record(replacement)
            record["status"] = "planned"
            record["adapted_from_status"] = status
            if self.can_write:
                self.state["sessions"][replacement.key] = record
            action = self._upload_and_schedule(replacement, record)
            if record.get("status") == "scheduled":
                record["status"] = "adapted"
            actions.append(action)

        if self.can_write:
            self.state["daily_adaptations"][today.isoformat()] = {
                "status": status,
                "reasons": reasons,
                "actions": actions,
                "evaluated_at": datetime.now().astimezone().isoformat(),
            }
            save_state(self.state_path, self.state)
        return {
            "status": "adapted" if self.can_write else "preview",
            "recovery_status": status,
            "reasons": reasons,
            "actions": actions,
        }

    def rebuild(self, today: date) -> dict[str, Any]:
        future_records = []
        for record in self.state["sessions"].values():
            session = record.get("session", {})
            raw_day = session.get("day")
            if raw_day and date.fromisoformat(raw_day) >= today and record.get(
                    "status"
            ) in {"scheduled", "adapted", "planned"}:
                future_records.append(record)

        if not self.can_write:
            return {
                "status": "preview_only",
                "note": (
                    "Rebuild did not change local state or Garmin. Enable writes and "
                    "disable DRY_RUN only after reviewing this list."
                ),
                "would_remove": [record.get("session", {}) for record in future_records],
            }

        actions: list[dict[str, Any]] = []
        for key, record in list(self.state["sessions"].items()):
            session = record.get("session", {})
            raw_day = session.get("day")
            if not raw_day or date.fromisoformat(raw_day) < today:
                continue
            if record.get("status") in {"scheduled", "adapted", "planned"}:
                actions.append(self._unschedule_record(record, "manual rebuild"))
            self.state["sessions"].pop(key, None)

            # Persist each completed removal. If Garmin or the network fails on a
            # later record, a retry resumes from the remaining entries instead of
            # trying to remove already-deleted schedules again.
            save_state(self.state_path, self.state)

        self.state["daily_adaptations"] = {
            key: value
            for key, value in self.state["daily_adaptations"].items()
            if date.fromisoformat(key) < today
        }
        save_state(self.state_path, self.state)
        plan_result = self.ensure_horizon(today, force_publish=True)
        return {"status": "applied", "removed": actions, "new_plan": plan_result}

    def status(self, today: date) -> dict[str, Any]:
        future = []
        for record in self.state["sessions"].values():
            session = record.get("session", {})
            if not session.get("day"):
                continue
            if date.fromisoformat(session["day"]) >= today:
                future.append(
                    {
                        "session": session,
                        "status": record.get("status"),
                        "workout_id": record.get("workout_id"),
                        "scheduled_workout_id": record.get(
                            "scheduled_workout_id"
                        ),
                    }
                )
        sorted_future = sorted(
            future,
            key=lambda item: (
                item["session"].get("day", ""),
                item["session"].get("slot_index", 0),
            ),
        )
        preview_sessions: list[dict[str, Any]] = []
        if not sorted_future:
            start = self._plan_start(today)
            base = int(
                self.state.get("effective_base_weekly_minutes")
                or self.config["plan"]["base_weekly_minutes"]
            )
            end = today + timedelta(days=int(self.config["plan"]["horizon_days"]) - 1)
            preview_sessions = [
                asdict(item)
                for item in build_sessions_for_range(
                    self.config, today, end, start, base
                )
            ]
        return {
            "active_coach_proposal_id": self.config.get("plan", {}).get("active_coach_proposal_id"),
            "coach_volume_multiplier": self.config.get("plan", {}).get("coach_volume_multiplier", 1.0),
            "sport_weight_multipliers": self.config.get("plan", {}).get("sport_weight_multipliers", {}),
            "quality_progression": self.config.get("plan", {}).get("quality_progression", {}),
            "publication": {
                "publish_weekday": self.config.get("plan", {}).get("publish_weekday", "sunday"),
                "publish_weeks": self.config.get("plan", {}).get("publish_weeks", 2),
                "last_publication": self.state.get("last_publication"),
            },
            "resolved_targets": self.config.get("_resolved_targets", {}),
            "plan_started_on": self.state.get("plan_started_on"),
            "effective_base_weekly_minutes": self.state.get(
                "effective_base_weekly_minutes"
            ),
            "recent_load": self.state.get("recent_load"),
            "future_sessions": sorted_future,
            "preview_sessions": preview_sessions,
        }

    def _prune_state(self, today: date) -> None:
        cutoff = today - timedelta(days=120)
        for key, record in list(self.state["sessions"].items()):
            raw = record.get("session", {}).get("day")
            if raw and date.fromisoformat(raw) < cutoff:
                self.state["sessions"].pop(key, None)
        for raw in list(self.state["daily_adaptations"]):
            if date.fromisoformat(raw) < cutoff:
                self.state["daily_adaptations"].pop(raw, None)
