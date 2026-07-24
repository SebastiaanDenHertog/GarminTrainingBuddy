from __future__ import annotations

import os
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

TARGET_MARKER = " Target guidance:"


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _float(value: Any, default: float) -> float:
    parsed = _number(value)
    return parsed if parsed is not None else default


def parse_pace_seconds(value: Any) -> float | None:
    """Parse seconds or an MM:SS pace into seconds per km/100 m."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if float(value) > 0 else None
    text = str(value).strip()
    if not text:
        return None
    if ":" not in text:
        return _number(text)
    parts = text.split(":")
    if len(parts) != 2:
        return None
    try:
        minutes = int(parts[0])
        seconds = float(parts[1])
    except ValueError:
        return None
    if minutes < 0 or not 0 <= seconds < 60:
        return None
    total = minutes * 60 + seconds
    return total if total > 0 else None


def format_pace(seconds: float | None, unit: str) -> str | None:
    if seconds is None or seconds <= 0:
        return None
    rounded = int(round(seconds))
    minutes, remainder = divmod(rounded, 60)
    separator = "" if unit.startswith("/") else " "
    return f"{minutes}:{remainder:02d}{separator}{unit}"


def _env_or_config(
    env_name: str,
    config: dict[str, Any],
    config_key: str,
    *,
    pace: bool = False,
) -> float | None:
    raw = os.getenv(env_name)
    value = raw if raw not in (None, "") else config.get(config_key)
    return parse_pace_seconds(value) if pace else _number(value)


def _latest_benchmark_rows(db_path: Path) -> dict[str, dict[str, Any]]:
    if not db_path.exists():
        return {}
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT benchmark_id, sport, day, result_value, result_unit,
                   secondary_value, secondary_unit, confidence, method
            FROM benchmark_results
            ORDER BY day DESC
            """
        ).fetchall()
    except (sqlite3.Error, OSError):
        return {}
    finally:
        if connection is not None:
            connection.close()

    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        benchmark_id = str(row["benchmark_id"] or "")
        if benchmark_id and benchmark_id not in latest:
            latest[benchmark_id] = dict(row)
    return latest


def _benchmark_is_usable(row: dict[str, Any], minimum_confidence: float) -> bool:
    confidence = _number(row.get("confidence"))
    return confidence is not None and confidence >= minimum_confidence


def resolve_training_targets(
    config: dict[str, Any], data_dir: Path | str
) -> dict[str, Any]:
    """Resolve manual and benchmark-derived workout targets.

    Targets are only accepted from explicit manual values or benchmark records
    above the configured confidence threshold. Arbitrary training activities are
    never promoted to threshold values.
    """
    target_cfg = config.get("workout_targets") or {}
    manual = target_cfg.get("manual") or {}
    conversions = target_cfg.get("benchmark_conversions") or {}
    enabled_raw = os.getenv("WORKOUT_TARGETS_ENABLED")
    enabled = (
        enabled_raw.strip().lower() in {"1", "true", "yes", "on"}
        if enabled_raw is not None
        else bool(target_cfg.get("enabled", True))
    )
    minimum_confidence = _float(
        os.getenv(
            "WORKOUT_TARGET_MIN_CONFIDENCE",
            conversions.get("minimum_confidence", 0.65),
        ),
        0.65,
    )
    run_20min_factor = _float(
        conversions.get("run_20min_to_threshold_pace_factor"), 1.03
    )
    bike_20min_factor = _float(
        conversions.get("bike_20min_to_ftp_factor"), 0.95
    )

    profile: dict[str, Any] = {
        "enabled": enabled,
        "minimum_benchmark_confidence": minimum_confidence,
        "run_threshold_pace_sec_per_km": _env_or_config(
            "RUN_THRESHOLD_PACE", manual, "run_threshold_pace", pace=True
        ),
        "run_threshold_hr_bpm": _env_or_config(
            "RUN_THRESHOLD_HR", manual, "run_threshold_hr_bpm"
        ),
        "run_aerobic_pace_sec_per_km": None,
        "run_aerobic_hr_bpm": None,
        "bike_ftp_watts": _env_or_config(
            "BIKE_FTP_WATTS", manual, "bike_ftp_watts"
        ),
        "bike_threshold_hr_bpm": _env_or_config(
            "BIKE_THRESHOLD_HR", manual, "bike_threshold_hr_bpm"
        ),
        "swim_css_sec_per_100m": _env_or_config(
            "SWIM_CSS_PACE", manual, "swim_css_pace", pace=True
        ),
        "sources": {},
        "source_confidence": {},
    }

    manual_keys = (
        "run_threshold_pace_sec_per_km",
        "run_threshold_hr_bpm",
        "bike_ftp_watts",
        "bike_threshold_hr_bpm",
        "swim_css_sec_per_100m",
    )
    for key in manual_keys:
        if profile[key] is not None:
            profile["sources"][key] = "manual/env"
            profile["source_confidence"][key] = 1.0

    db_path = Path(
        os.getenv("TRAINING_DB_PATH", str(Path(data_dir) / "training.db"))
    )
    latest = _latest_benchmark_rows(db_path)

    run_threshold = latest.get("run_threshold_20") or {}
    if _benchmark_is_usable(run_threshold, minimum_confidence):
        confidence = float(run_threshold["confidence"])
        if profile["run_threshold_pace_sec_per_km"] is None:
            value = _number(run_threshold.get("result_value"))
            if value and run_threshold.get("result_unit") == "sec/km":
                profile["run_threshold_pace_sec_per_km"] = round(
                    value * run_20min_factor, 2
                )
                profile["sources"]["run_threshold_pace_sec_per_km"] = (
                    f"run_threshold_20 × {run_20min_factor:.3f}"
                )
                profile["source_confidence"][
                    "run_threshold_pace_sec_per_km"
                ] = confidence
        if profile["run_threshold_hr_bpm"] is None:
            value = _number(run_threshold.get("secondary_value"))
            if value and run_threshold.get("secondary_unit") == "bpm":
                profile["run_threshold_hr_bpm"] = value
                profile["sources"]["run_threshold_hr_bpm"] = "run_threshold_20"
                profile["source_confidence"]["run_threshold_hr_bpm"] = confidence

    run_aerobic = latest.get("run_aerobic_30") or {}
    if _benchmark_is_usable(run_aerobic, minimum_confidence):
        aerobic_hr = _number(run_aerobic.get("secondary_value"))
        efficiency = _number(run_aerobic.get("result_value"))
        if (
            aerobic_hr
            and efficiency
            and run_aerobic.get("result_unit") == "m/s per bpm"
        ):
            speed = aerobic_hr * efficiency
            if speed > 0:
                confidence = float(run_aerobic["confidence"])
                profile["run_aerobic_pace_sec_per_km"] = 1000.0 / speed
                profile["run_aerobic_hr_bpm"] = aerobic_hr
                profile["sources"]["run_aerobic_pace_sec_per_km"] = (
                    "run_aerobic_30"
                )
                profile["sources"]["run_aerobic_hr_bpm"] = "run_aerobic_30"
                profile["source_confidence"][
                    "run_aerobic_pace_sec_per_km"
                ] = confidence
                profile["source_confidence"]["run_aerobic_hr_bpm"] = confidence

    bike_threshold = latest.get("bike_threshold_20") or {}
    if _benchmark_is_usable(bike_threshold, minimum_confidence):
        confidence = float(bike_threshold["confidence"])
        if profile["bike_ftp_watts"] is None:
            value = _number(bike_threshold.get("result_value"))
            if value and bike_threshold.get("result_unit") == "W":
                profile["bike_ftp_watts"] = round(value * bike_20min_factor, 1)
                profile["sources"]["bike_ftp_watts"] = (
                    f"bike_threshold_20 × {bike_20min_factor:.3f}"
                )
                profile["source_confidence"]["bike_ftp_watts"] = confidence
        if profile["bike_threshold_hr_bpm"] is None:
            value = _number(bike_threshold.get("secondary_value"))
            if value and bike_threshold.get("secondary_unit") == "bpm":
                profile["bike_threshold_hr_bpm"] = value
                profile["sources"]["bike_threshold_hr_bpm"] = (
                    "bike_threshold_20"
                )
                profile["source_confidence"][
                    "bike_threshold_hr_bpm"
                ] = confidence
            else:
                value = _number(bike_threshold.get("result_value"))
                if value and bike_threshold.get("result_unit") == "bpm":
                    profile["bike_threshold_hr_bpm"] = value
                    profile["sources"]["bike_threshold_hr_bpm"] = (
                        "bike_threshold_20"
                    )
                    profile["source_confidence"][
                        "bike_threshold_hr_bpm"
                    ] = confidence

    swim_css = latest.get("swim_css_400_200") or {}
    if (
        profile["swim_css_sec_per_100m"] is None
        and _benchmark_is_usable(swim_css, minimum_confidence)
    ):
        value = _number(swim_css.get("result_value"))
        if value and swim_css.get("result_unit") == "sec/100m":
            profile["swim_css_sec_per_100m"] = value
            profile["sources"]["swim_css_sec_per_100m"] = "swim_css_400_200"
            profile["source_confidence"]["swim_css_sec_per_100m"] = float(
                swim_css["confidence"]
            )

    profile["database_path"] = str(db_path)
    return profile


def _pair(values: Any, fallback: tuple[float, float]) -> tuple[float, float]:
    if isinstance(values, Iterable) and not isinstance(values, (str, bytes, dict)):
        parsed = [_number(item) for item in list(values)[:2]]
        if len(parsed) == 2 and parsed[0] is not None and parsed[1] is not None:
            return float(parsed[0]), float(parsed[1])
    return fallback


def _pace_range(
    base_seconds: float, multipliers: tuple[float, float]
) -> tuple[float, float]:
    values = sorted(base_seconds * item for item in multipliers)
    return values[0], values[1]


def _hr_range(base_hr: float, percentages: tuple[float, float]) -> tuple[int, int]:
    values = sorted(int(round(base_hr * item)) for item in percentages)
    return values[0], values[1]


def _power_range(ftp: float, percentages: tuple[float, float]) -> tuple[int, int]:
    values = sorted(int(round(ftp * item)) for item in percentages)
    return values[0], values[1]


def _pace_target(
    fast_sec: float,
    slow_sec: float,
    summary: str,
    secondary: str | None,
    source: str | None,
) -> dict[str, Any]:
    low_speed, high_speed = sorted((1000.0 / slow_sec, 1000.0 / fast_sec))
    return {
        "kind": "pace",
        "low": round(low_speed, 4),
        "high": round(high_speed, 4),
        "unit": "m/s",
        "summary": summary,
        "secondary_summary": secondary,
        "source": source,
    }


def _hr_target(
    low: int,
    high: int,
    summary: str,
    secondary: str | None,
    source: str | None,
) -> dict[str, Any]:
    return {
        "kind": "heart_rate",
        "low": low,
        "high": high,
        "unit": "bpm",
        "summary": summary,
        "secondary_summary": secondary,
        "source": source,
    }


def target_for_session(config: dict[str, Any], session: Any) -> dict[str, Any] | None:
    profile = config.get("_resolved_targets") or {}
    if not profile.get("enabled") or getattr(session, "benchmark_id", None):
        return None

    ranges = (config.get("workout_targets") or {}).get("ranges") or {}
    sport = str(getattr(session, "sport", ""))
    role = str(getattr(session, "role", ""))
    phase = str(getattr(session, "phase", ""))
    sources = profile.get("sources", {})

    if sport == "run":
        threshold_pace = _number(profile.get("run_threshold_pace_sec_per_km"))
        threshold_hr = _number(profile.get("run_threshold_hr_bpm"))
        aerobic_pace = _number(profile.get("run_aerobic_pace_sec_per_km"))
        aerobic_hr = _number(profile.get("run_aerobic_hr_bpm"))

        if role == "quality":
            phase_cfg = ((ranges.get("run_quality") or {}).get(phase) or {})
            if threshold_pace:
                multipliers = _pair(
                    phase_cfg.get("pace_multiplier"),
                    {
                        "foundation": (0.94, 1.02),
                        "aerobic_build": (0.98, 1.04),
                        "vo2_economy": (0.88, 0.95),
                        "durability": (0.97, 1.02),
                    }.get(phase, (0.95, 1.03)),
                )
                fast_sec, slow_sec = _pace_range(threshold_pace, multipliers)
                secondary = (
                    f"HR reference about {int(round(threshold_hr))} bpm"
                    if threshold_hr
                    else None
                )
                return _pace_target(
                    fast_sec,
                    slow_sec,
                    f"Run intervals {format_pace(fast_sec, '/km')}–{format_pace(slow_sec, '/km')}",
                    secondary,
                    sources.get("run_threshold_pace_sec_per_km"),
                )
            if threshold_hr:
                percentages = _pair(
                    phase_cfg.get("hr_pct_lthr"),
                    {
                        "foundation": (0.90, 0.98),
                        "aerobic_build": (0.94, 1.00),
                        "vo2_economy": (0.95, 1.02),
                        "durability": (0.90, 0.97),
                    }.get(phase, (0.92, 0.99)),
                )
                low, high = _hr_range(threshold_hr, percentages)
                return _hr_target(
                    low,
                    high,
                    f"Run interval HR {low}–{high} bpm",
                    "HR rises slowly; use controlled RPE during short intervals",
                    sources.get("run_threshold_hr_bpm"),
                )

        role_key = {
            "recovery": "run_recovery",
            "technique": "run_recovery",
            "easy": "run_easy",
            "brick": "run_brick",
            "long": "run_long",
            "endurance": "run_long",
        }.get(role, "run_easy")
        role_cfg = ranges.get(role_key) or {}
        defaults = {
            "run_recovery": ((0.70, 0.80), (1.28, 1.48)),
            "run_easy": ((0.75, 0.85), (1.20, 1.40)),
            "run_brick": ((0.76, 0.86), (1.18, 1.38)),
            "run_long": ((0.78, 0.88), (1.15, 1.35)),
        }
        hr_default, pace_default = defaults[role_key]
        pace_base = threshold_pace or aerobic_pace
        if threshold_hr:
            low, high = _hr_range(
                threshold_hr, _pair(role_cfg.get("hr_pct_lthr"), hr_default)
            )
            secondary = None
            if pace_base:
                fast_sec, slow_sec = _pace_range(
                    pace_base,
                    _pair(role_cfg.get("pace_multiplier"), pace_default),
                )
                secondary = (
                    f"pace guide {format_pace(fast_sec, '/km')}–"
                    f"{format_pace(slow_sec, '/km')}"
                )
            return _hr_target(
                low,
                high,
                f"Run HR {low}–{high} bpm",
                secondary,
                sources.get("run_threshold_hr_bpm"),
            )
        if pace_base:
            fast_sec, slow_sec = _pace_range(
                pace_base, _pair(role_cfg.get("pace_multiplier"), pace_default)
            )
            return _pace_target(
                fast_sec,
                slow_sec,
                f"Run pace {format_pace(fast_sec, '/km')}–{format_pace(slow_sec, '/km')}",
                None,
                sources.get("run_threshold_pace_sec_per_km")
                or sources.get("run_aerobic_pace_sec_per_km"),
            )
        if aerobic_hr:
            spread = int((ranges.get("run_aerobic_benchmark") or {}).get("hr_spread_bpm", 5))
            low = max(1, int(round(aerobic_hr)) - spread)
            high = int(round(aerobic_hr)) + spread
            return _hr_target(low, high, f"Run HR {low}–{high} bpm", None, "run_aerobic_30")

    if sport == "bike":
        ftp = _number(profile.get("bike_ftp_watts"))
        threshold_hr = _number(profile.get("bike_threshold_hr_bpm"))
        if role == "quality":
            phase_cfg = ((ranges.get("bike_quality") or {}).get(phase) or {})
            if ftp:
                percentages = _pair(
                    phase_cfg.get("ftp_pct"),
                    {
                        "foundation": (0.88, 1.00),
                        "aerobic_build": (0.95, 1.02),
                        "vo2_economy": (1.06, 1.18),
                        "durability": (0.88, 0.96),
                    }.get(phase, (0.90, 1.02)),
                )
                low, high = _power_range(ftp, percentages)
                secondary = (
                    f"HR reference about {int(round(threshold_hr))} bpm"
                    if threshold_hr
                    else None
                )
                return {
                    "kind": "power",
                    "low": low,
                    "high": high,
                    "unit": "W",
                    "summary": f"Bike intervals {low}–{high} W",
                    "secondary_summary": secondary,
                    "source": sources.get("bike_ftp_watts"),
                }
            if threshold_hr:
                percentages = _pair(
                    phase_cfg.get("hr_pct_lthr"),
                    {
                        "foundation": (0.88, 0.97),
                        "aerobic_build": (0.92, 1.00),
                        "vo2_economy": (0.95, 1.02),
                        "durability": (0.88, 0.96),
                    }.get(phase, (0.90, 0.98)),
                )
                low, high = _hr_range(threshold_hr, percentages)
                return _hr_target(
                    low,
                    high,
                    f"Bike interval HR {low}–{high} bpm",
                    "HR rises slowly; use controlled RPE during short intervals",
                    sources.get("bike_threshold_hr_bpm"),
                )

        role_key = "bike_easy" if role in {"easy", "recovery"} else "bike_long"
        role_cfg = ranges.get(role_key) or {}
        hr_default = (0.72, 0.84) if role_key == "bike_easy" else (0.75, 0.87)
        power_default = (0.55, 0.72) if role_key == "bike_easy" else (0.60, 0.76)
        if threshold_hr:
            low, high = _hr_range(
                threshold_hr, _pair(role_cfg.get("hr_pct_lthr"), hr_default)
            )
            secondary = None
            if ftp:
                power_low, power_high = _power_range(
                    ftp, _pair(role_cfg.get("ftp_pct"), power_default)
                )
                secondary = f"power guide {power_low}–{power_high} W"
            return _hr_target(
                low,
                high,
                f"Bike HR {low}–{high} bpm",
                secondary,
                sources.get("bike_threshold_hr_bpm"),
            )
        if ftp:
            low, high = _power_range(
                ftp, _pair(role_cfg.get("ftp_pct"), power_default)
            )
            return {
                "kind": "power",
                "low": low,
                "high": high,
                "unit": "W",
                "summary": f"Bike power {low}–{high} W",
                "secondary_summary": None,
                "source": sources.get("bike_ftp_watts"),
            }

    if sport == "swim":
        css = _number(profile.get("swim_css_sec_per_100m"))
        if css:
            offsets = {
                "technique": (15, 25),
                "easy": (12, 22),
                "recovery": (18, 28),
                "endurance": (8, 15),
                "long": (8, 15),
                "quality": (-2, 5),
            }.get(role, (10, 20))
            fast_sec, slow_sec = sorted((css + offsets[0], css + offsets[1]))
            return {
                "kind": "description_only",
                "low": fast_sec,
                "high": slow_sec,
                "unit": "sec/100m",
                "summary": (
                    f"Swim pace {format_pace(fast_sec, '/100m')}–"
                    f"{format_pace(slow_sec, '/100m')}"
                ),
                "secondary_summary": None,
                "source": sources.get("swim_css_sec_per_100m"),
            }

    return None


def _clean_generated_target(description: str) -> str:
    return description.split(TARGET_MARKER, 1)[0].rstrip()


def annotate_sessions(config: dict[str, Any], sessions: list[Any]) -> list[Any]:
    output = []
    for session in sessions:
        target = target_for_session(config, session)
        description = _clean_generated_target(str(session.description))
        if target:
            summary = target.get("summary")
            secondary = target.get("secondary_summary")
            source = target.get("source")
            details = ". ".join(item for item in (summary, secondary) if item)
            if details:
                description += f"{TARGET_MARKER} {details}."
            if source:
                description += f" Target source: {source}."
            output.append(
                replace(
                    session,
                    description=description,
                    target_kind=str(target.get("kind")),
                    target_low=(
                        float(target["low"])
                        if target.get("low") is not None
                        else None
                    ),
                    target_high=(
                        float(target["high"])
                        if target.get("high") is not None
                        else None
                    ),
                    target_unit=(
                        str(target.get("unit")) if target.get("unit") else None
                    ),
                    target_summary=str(summary) if summary else None,
                    target_source=str(source) if source else None,
                )
            )
        else:
            output.append(
                replace(
                    session,
                    description=description,
                    target_kind=None,
                    target_low=None,
                    target_high=None,
                    target_unit=None,
                    target_summary=None,
                    target_source=None,
                )
            )
    return output
