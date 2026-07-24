from __future__ import annotations

import json
import math
import os
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable


PROTOCOLS: dict[str, dict[str, Any]] = {
    "run_aerobic_30": {
        "sport": "run",
        "label": "30-minute run aerobic efficiency",
        "intensity": "moderate",
        "default_duration_minutes": 50,
        "description": (
            "Run the same flat route when possible. Warm up 10 minutes, then run "
            "30 minutes at steady conversational effort (RPE 4/10), then cool down. "
            "Do not turn this into a race."
        ),
    },
    "run_threshold_20": {
        "sport": "run",
        "label": "20-minute run threshold benchmark",
        "intensity": "hard",
        "default_duration_minutes": 55,
        "description": (
            "After a progressive warm-up, complete 20 minutes at the fastest even "
            "effort you can sustain without fading. Use a flat, repeatable route."
        ),
    },
    "bike_aerobic_45": {
        "sport": "bike",
        "label": "45-minute bike aerobic efficiency",
        "intensity": "moderate",
        "default_duration_minutes": 70,
        "description": (
            "Use the same indoor setup or flat route. After warming up, ride 45 "
            "minutes steadily at RPE 4/10 with constant cadence and no surges."
        ),
    },
    "bike_threshold_20": {
        "sport": "bike",
        "label": "20-minute bike threshold benchmark",
        "intensity": "hard",
        "default_duration_minutes": 60,
        "description": (
            "After a full warm-up, complete 20 minutes at the highest even power "
            "you can sustain. Avoid starting too hard. Prefer an indoor trainer or "
            "an uninterrupted route."
        ),
    },
    "swim_css_400_200": {
        "sport": "swim",
        "label": "CSS swim benchmark",
        "intensity": "hard",
        "default_duration_minutes": 60,
        "description": (
            "After warming up, swim one controlled maximal long effort, recover "
            "fully, then one controlled maximal short effort. The distances are "
            "rounded to whole pool lengths. Record each effort as a separate lap."
        ),
    },
}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def settings(config: dict[str, Any]) -> dict[str, Any]:
    measurement = config.get("measurement") or {}
    configured = measurement.get("benchmarks") or {}
    enabled = _env_bool("BENCHMARKS_ENABLED", bool(configured.get("enabled", False)))
    auto_schedule = _env_bool(
        "BENCHMARK_AUTO_SCHEDULE", bool(configured.get("auto_schedule", False))
    )
    sports = configured.get("sports") or {
        "run": {"protocol": "run_aerobic_30", "offset_weeks": 0},
        "bike": {"protocol": "bike_aerobic_45", "offset_weeks": 1},
        "swim": {"protocol": "swim_css_400_200", "offset_weeks": 2},
    }
    return {
        "enabled": enabled,
        "auto_schedule": auto_schedule,
        "interval_weeks": max(4, int(configured.get("interval_weeks", 8))),
        "first_test_week": max(0, int(configured.get("first_test_week", 2))),
        "sports": sports,
    }


def due_protocols(config: dict[str, Any], week_index: int) -> list[tuple[str, str]]:
    cfg = settings(config)
    if not cfg["enabled"] or not cfg["auto_schedule"]:
        return []
    if week_index < cfg["first_test_week"]:
        return []
    due: list[tuple[str, str]] = []
    for sport, sport_cfg in cfg["sports"].items():
        if not isinstance(sport_cfg, dict):
            continue
        protocol = str(sport_cfg.get("protocol") or "")
        if protocol not in PROTOCOLS or PROTOCOLS[protocol]["sport"] != sport:
            continue
        offset = int(sport_cfg.get("offset_weeks", 0))
        first = cfg["first_test_week"] + offset
        if week_index < first:
            continue
        cycle = max(0, (week_index - first) // cfg["interval_weeks"])
        for candidate_cycle in {max(0, cycle - 1), cycle, cycle + 1}:
            nominal = first + candidate_cycle * cfg["interval_weeks"]
            scheduled = nominal + 1 if nominal % 4 == 3 else nominal
            if week_index == scheduled:
                due.append((sport, protocol))
                break
    return due


def apply_due_benchmark(
    config: dict[str, Any],
    sessions: list[Any],
    *,
    week_index: int,
    recovery_week: bool,
) -> list[Any]:
    if recovery_week:
        return sessions
    due = due_protocols(config, week_index)
    if not due:
        return sessions

    result = list(sessions)
    configured_sports = settings(config)["sports"]
    for sport, protocol_id in due:
        sport_cfg = configured_sports.get(sport) or {}
        preferred_weekday = str(sport_cfg.get("preferred_weekday") or "").lower()
        replace_roles = {
            str(value)
            for value in (sport_cfg.get("replace_roles") or ["quality", "endurance", "easy"])
        }
        candidates = [
            (index, session)
            for index, session in enumerate(result)
            if getattr(session, "sport", None) == sport
            and getattr(session, "role", None) in replace_roles
        ]
        if preferred_weekday:
            preferred = [
                item
                for item in candidates
                if date.fromisoformat(getattr(item[1], "day")).strftime("%A").lower()
                == preferred_weekday
            ]
            if preferred:
                candidates = preferred
        if not candidates:
            continue
        index, session = sorted(
            candidates,
            key=lambda item: (
                not bool(getattr(item[1], "primary", True)),
                -int(getattr(item[1], "duration_minutes", 0)),
            ),
        )[0]
        protocol = PROTOCOLS[protocol_id]
        duration = max(
            int(getattr(session, "duration_minutes", 0)),
            int(protocol["default_duration_minutes"]),
        )
        prefix = str(config.get("plan", {}).get("name_prefix", "AI BASE"))
        label = str(protocol["label"])
        description = (
            f"Measurement benchmark {protocol_id}. {protocol['description']} "
            "Keep conditions as repeatable as practical. The result is stored in "
            "the local measurement database after the activity syncs from Garmin."
        )
        benchmark_session = replace(
            session,
            role="benchmark",
            duration_minutes=duration,
            name=f"{prefix} · {label} · {duration} min",
            description=description,
            intensity=str(protocol["intensity"]),
            benchmark_id=protocol_id,
        )
        result[index] = benchmark_session

        # A hard benchmark replaces the purpose of another quality session when
        # necessary; it must never silently exceed the configured hard-session
        # limits merely because benchmark insertion happens after base planning.
        if benchmark_session.intensity == "hard":
            max_hard_week = int(config.get("plan", {}).get("max_hard_sessions_per_week", 2))
            max_hard_day = int(config.get("plan", {}).get("max_hard_sessions_per_day", 1))
            benchmark_day = date.fromisoformat(benchmark_session.day)

            def hard_indices() -> list[int]:
                return [
                    item_index
                    for item_index, item in enumerate(result)
                    if getattr(item, "intensity", None) == "hard"
                ]

            def downgrade(candidate_index: int) -> None:
                candidate = result[candidate_index]
                easy_role = "technique" if getattr(candidate, "sport", None) == "swim" else "easy"
                candidate_duration = int(getattr(candidate, "duration_minutes", 0))
                result[candidate_index] = replace(
                    candidate,
                    role=easy_role,
                    intensity="easy",
                    name=(
                        f"{prefix} · Benchmark support · "
                        f"{str(getattr(candidate, 'sport', '')).title()} easy · "
                        f"{candidate_duration} min"
                    ),
                    description=(
                        "This quality session was changed to easy training so the "
                        "measurement week stays within the configured hard-session "
                        "limit. Complete it conversationally and do not add intervals."
                    ),
                )

            same_day_other_hard = [
                item_index
                for item_index in hard_indices()
                if item_index != index
                and getattr(result[item_index], "day", None) == benchmark_session.day
            ]
            while same_day_other_hard and sum(
                getattr(item, "day", None) == benchmark_session.day
                and getattr(item, "intensity", None) == "hard"
                for item in result
            ) > max_hard_day:
                downgrade(same_day_other_hard.pop(0))

            while len(hard_indices()) > max_hard_week:
                alternatives = [
                    item_index
                    for item_index in hard_indices()
                    if item_index != index and not getattr(result[item_index], "benchmark_id", None)
                ]
                if not alternatives:
                    # Refuse to violate the safety limit if all hard sessions are
                    # protected benchmarks. Restore the original session instead.
                    result[index] = session
                    break
                alternatives.sort(
                    key=lambda item_index: abs(
                        (date.fromisoformat(result[item_index].day) - benchmark_day).days
                    )
                )
                downgrade(alternatives[0])
    return result


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _lap_candidates(payload: Any) -> list[dict[str, float]]:
    results: list[dict[str, float]] = []

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 7:
            return
        if isinstance(node, dict):
            duration = _number(
                node.get("duration")
                or node.get("elapsedDuration")
                or node.get("movingDuration")
                or node.get("sumDuration")
            )
            distance = _number(node.get("distance") or node.get("sumDistance"))
            speed = _number(node.get("averageSpeed") or node.get("avgSpeed"))
            hr = _number(node.get("averageHR") or node.get("averageHeartRate"))
            power = _number(node.get("averagePower") or node.get("avgPower"))
            if duration and duration > 10 and (distance or speed or power or hr):
                results.append(
                    {
                        "duration_seconds": duration,
                        "distance_m": distance or 0.0,
                        "average_speed_mps": speed or 0.0,
                        "average_hr": hr or 0.0,
                        "average_power": power or 0.0,
                    }
                )
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node[:500]:
                walk(value, depth + 1)

    walk(payload)
    unique: dict[tuple[float, float, float], dict[str, float]] = {}
    for item in results:
        key = (
            round(item["duration_seconds"], 1),
            round(item["distance_m"], 1),
            round(item["average_power"], 1),
        )
        unique[key] = item
    return list(unique.values())


def _cached_payload(data_dir: Path, activity_id: int) -> dict[str, Any]:
    path = data_dir / "cache" / "activities" / f"{activity_id}.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _lap_near_duration(laps: list[dict[str, float]], target: float) -> dict[str, float] | None:
    candidates = [item for item in laps if target * 0.65 <= item["duration_seconds"] <= target * 1.35]
    return min(candidates, key=lambda item: abs(item["duration_seconds"] - target)) if candidates else None


def _lap_near_distance(laps: list[dict[str, float]], target: float, tolerance: float) -> dict[str, float] | None:
    candidates = [item for item in laps if abs(item["distance_m"] - target) <= tolerance]
    return min(candidates, key=lambda item: abs(item["distance_m"] - target)) if candidates else None


def evaluate_result(
    data_dir: Path,
    session: dict[str, Any],
    sample: Any,
    *,
    pool_length_m: int = 25,
) -> dict[str, Any] | None:
    benchmark_id = str(session.get("benchmark_id") or "")
    if benchmark_id not in PROTOCOLS:
        return None
    activity_id = getattr(sample, "activity_id", None)
    if not activity_id:
        return None
    payload = _cached_payload(data_dir, int(activity_id))
    laps = _lap_candidates(payload)
    sport = str(getattr(sample, "sport"))
    result: dict[str, Any] = {
        "benchmark_id": benchmark_id,
        "sport": sport,
        "activity_id": int(activity_id),
        "session_key": session.get("key"),
        "day": str(getattr(sample, "day")),
        "confidence": 0.45,
        "method": "activity_summary",
        "details": {"lap_candidates": len(laps)},
    }

    if benchmark_id == "run_aerobic_30":
        result.update(
            result_value=getattr(sample, "efficiency_score", None),
            result_unit="m/s per bpm",
            secondary_value=getattr(sample, "average_hr", None),
            secondary_unit="bpm",
        )
    elif benchmark_id == "bike_aerobic_45":
        result.update(
            result_value=getattr(sample, "efficiency_score", None),
            result_unit="W per bpm",
            secondary_value=getattr(sample, "average_power", None),
            secondary_unit="W",
        )
    elif benchmark_id in {"run_threshold_20", "bike_threshold_20"}:
        lap = _lap_near_duration(laps, 20 * 60)
        if benchmark_id.startswith("run"):
            speed = (lap or {}).get("average_speed_mps") or getattr(sample, "average_speed_mps", None)
            if not speed:
                return None
            result.update(
                result_value=round(1000 / float(speed), 2),
                result_unit="sec/km",
                secondary_value=(lap or {}).get("average_hr") or getattr(sample, "average_hr", None),
                secondary_unit="bpm",
            )
        else:
            power = (lap or {}).get("average_power") or getattr(sample, "average_power", None)
            heart_rate = (lap or {}).get("average_hr") or getattr(sample, "average_hr", None)
            if power:
                result.update(
                    result_value=round(float(power), 1),
                    result_unit="W",
                    secondary_value=heart_rate,
                    secondary_unit="bpm",
                )
            elif heart_rate:
                # Riders without a power meter can still calibrate cycling HR.
                result.update(
                    result_value=round(float(heart_rate), 1),
                    result_unit="bpm",
                    secondary_value=None,
                    secondary_unit=None,
                )
            else:
                return None
        if lap:
            result["confidence"] = 0.9
            result["method"] = "matching_20_minute_lap"
            result["details"]["selected_lap"] = lap
    elif benchmark_id == "swim_css_400_200":
        long_distance = max(pool_length_m * 8, round(400 / pool_length_m) * pool_length_m)
        short_distance = max(pool_length_m * 4, round(200 / pool_length_m) * pool_length_m)
        tolerance = max(5.0, pool_length_m * 0.25)
        long_lap = _lap_near_distance(laps, long_distance, tolerance)
        short_lap = _lap_near_distance(laps, short_distance, tolerance)
        if not long_lap or not short_lap:
            speed = getattr(sample, "average_speed_mps", None)
            if not speed:
                return None
            result.update(
                result_value=round(100 / float(speed), 2),
                result_unit="sec/100m",
                secondary_value=None,
                secondary_unit=None,
            )
        else:
            time_difference = long_lap["duration_seconds"] - short_lap["duration_seconds"]
            distance_difference = long_lap["distance_m"] - short_lap["distance_m"]
            if time_difference <= 0 or distance_difference <= 0:
                return None
            css_speed = distance_difference / time_difference
            result.update(
                result_value=round(100 / css_speed, 2),
                result_unit="sec/100m",
                secondary_value=round(css_speed, 4),
                secondary_unit="m/s",
                confidence=0.95,
                method="css_two_lap_calculation",
            )
            result["details"].update(
                {"long_effort": long_lap, "short_effort": short_lap}
            )
    else:
        return None

    if result.get("result_value") is None:
        return None
    return result


def due_status(config: dict[str, Any], latest_results: Iterable[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    cfg = settings(config)
    latest_by_sport: dict[str, date] = {}
    for item in latest_results:
        try:
            when = date.fromisoformat(str(item.get("day")))
        except (TypeError, ValueError):
            continue
        sport = str(item.get("sport") or "")
        latest_by_sport[sport] = max(when, latest_by_sport.get(sport, when))
    interval = timedelta(weeks=cfg["interval_weeks"])
    output: list[dict[str, Any]] = []
    for sport, sport_cfg in cfg["sports"].items():
        protocol = str((sport_cfg or {}).get("protocol") or "")
        last = latest_by_sport.get(sport)
        due = last + interval if last else today
        output.append(
            {
                "sport": sport,
                "protocol": protocol,
                "label": PROTOCOLS.get(protocol, {}).get("label", protocol),
                "last_completed": last.isoformat() if last else None,
                "next_due": due.isoformat(),
                "due": due <= today,
                "enabled": cfg["enabled"],
                "auto_schedule": cfg["auto_schedule"],
            }
        )
    return output
