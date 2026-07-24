from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

SPORTS = ("run", "bike", "swim", "strength")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _latest_json(directory: Path) -> tuple[Path | None, dict[str, Any] | None]:
    if not directory.exists():
        return None, None
    candidates = sorted(directory.glob("*.json"), key=lambda item: item.stat().st_mtime)
    for path in reversed(candidates):
        payload = _read_json(path)
        if payload is not None:
            return path, payload
    return None, None


def _latest_proposal(data_dir: Path) -> tuple[Path | None, dict[str, Any] | None]:
    return _latest_json(data_dir / "coach" / "proposals")


def _session_source(data_dir: Path, report: dict[str, Any] | None) -> list[dict[str, Any]]:
    report_sessions = (((report or {}).get("plan") or {}).get("planned_sessions") or [])
    if isinstance(report_sessions, list) and report_sessions:
        return [item for item in report_sessions if isinstance(item, dict)]

    state = _read_json(data_dir / "planner_state.json") or {}
    sessions: list[dict[str, Any]] = []
    for record in (state.get("sessions") or {}).values():
        if not isinstance(record, dict):
            continue
        if record.get("status") in {"cancelled", "unscheduled"}:
            continue
        session = record.get("session")
        if isinstance(session, dict):
            sessions.append(session)
    return sessions


def _upcoming_sessions(
    sessions: list[dict[str, Any]], today: date, days: int = 21
) -> list[dict[str, Any]]:
    end = today + timedelta(days=max(1, days) - 1)
    result: list[dict[str, Any]] = []
    for item in sessions:
        try:
            when = date.fromisoformat(str(item.get("day")))
        except (TypeError, ValueError):
            continue
        if not today <= when <= end:
            continue
        result.append(
            {
                "day": when.isoformat(),
                "weekday": when.strftime("%A"),
                "sport": str(item.get("sport", "unknown")),
                "role": str(item.get("role", "")),
                "name": str(item.get("name", "Workout")),
                "duration_minutes": int(item.get("duration_minutes") or 0),
                "intensity": str(item.get("intensity", "")),
                "scheduled_time": item.get("scheduled_time"),
                "primary": bool(item.get("primary", True)),
                "strength_template": item.get("strength_template"),
                "strength_exercises": item.get("strength_exercises") or [],
            }
        )
    return sorted(
        result,
        key=lambda item: (
            item["day"],
            item.get("scheduled_time") or "99:99",
            not item.get("primary", True),
        ),
    )


def _next_seven_day_totals(sessions: list[dict[str, Any]], today: date) -> dict[str, int]:
    end = today + timedelta(days=6)
    totals = {sport: 0 for sport in SPORTS}
    for item in sessions:
        try:
            when = date.fromisoformat(str(item.get("day")))
        except (TypeError, ValueError):
            continue
        sport = str(item.get("sport", ""))
        if today <= when <= end and sport in totals:
            totals[sport] += int(item.get("duration_minutes") or 0)
    totals["all"] = sum(totals.values())
    totals["endurance"] = sum(totals[sport] for sport in ("run", "bike", "swim"))
    return totals


def _status_from_report(report: dict[str, Any] | None) -> dict[str, Any]:
    report = report or {}
    advice = report.get("advice") or {}
    metrics = report.get("metrics") or {}
    return {
        "status": str(advice.get("status", "UNKNOWN")),
        "recommendation": advice.get("recommendation"),
        "reasons": advice.get("reasons") or [],
        "day": metrics.get("day"),
        "sleep_hours": metrics.get("sleep_hours"),
        "sleep_score": metrics.get("sleep_score"),
        "training_readiness": metrics.get("training_readiness"),
        "training_readiness_level": metrics.get("training_readiness_level"),
        "hrv_ratio": metrics.get("hrv_ratio"),
        "resting_hr": metrics.get("resting_hr"),
        "resting_hr_delta": metrics.get("resting_hr_delta"),
        "subjective_feedback": metrics.get("subjective_feedback") or {},
    }


def _coach_summary(proposal: dict[str, Any] | None) -> dict[str, Any] | None:
    if not proposal:
        return None
    raw = proposal.get("proposal") if isinstance(proposal.get("proposal"), dict) else proposal
    validation = proposal.get("validation") or {}
    return {
        "decision": raw.get("decision"),
        "confidence": raw.get("confidence"),
        "source": raw.get("source"),
        "weekly_volume_change_percent": raw.get("weekly_volume_change_percent"),
        "sport_volume_change_percent": raw.get("sport_volume_change_percent") or {},
        "quality_changes": raw.get("quality_changes") or [],
        "reasoning_summary": raw.get("reasoning_summary") or [],
        "accepted": validation.get("accepted"),
        "warnings": validation.get("warnings") or [],
        "errors": validation.get("errors") or [],
        "applied": bool(proposal.get("applied")),
        "shadow_mode": bool(proposal.get("shadow_mode")),
        "generated_at": raw.get("generated_at") or proposal.get("generated_at"),
    }


def build_dashboard_payload() -> dict[str, Any]:
    data_dir = Path(os.getenv("DATA_DIR", "/data"))
    today = date.today()

    report_path, report = _latest_json(data_dir / "reports")
    analysis_path, analysis = _latest_json(data_dir / "analytics")
    proposal_path, proposal = _latest_proposal(data_dir)
    sessions = _session_source(data_dir, report)
    upcoming = _upcoming_sessions(sessions, today)

    try:
        from local_ai import ram_check_payload

        ai_ram = ram_check_payload()
    except Exception as exc:  # dashboard must remain available if AI checks fail
        ai_ram = {"error": str(exc), "enough_ram": False, "load_allowed": False}

    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "today": today.isoformat(),
        "recovery": _status_from_report(report),
        "next_7_days": _next_seven_day_totals(upcoming, today),
        "upcoming_sessions": upcoming,
        "analysis": analysis or {},
        "coach": _coach_summary(proposal),
        "ai": {
            "enabled": os.getenv("AI_ENABLED", "false").lower() in {"1", "true", "yes", "on"},
            "backend": os.getenv("AI_BACKEND", "embedded"),
            "model": os.getenv("AI_MODEL", "Qwen/Qwen3-30B-A3B"),
            "shadow_mode": os.getenv("AI_SHADOW_MODE", "true").lower() in {"1", "true", "yes", "on"},
            "ram": ai_ram,
        },
        "planner": {
            "garmin_write_enabled": os.getenv("GARMIN_WRITE_ENABLED", "false").lower()
            in {"1", "true", "yes", "on"},
            "dry_run": os.getenv("DRY_RUN", "true").lower()
            in {"1", "true", "yes", "on"},
        },
        "sources": {
            "report": str(report_path) if report_path else None,
            "analysis": str(analysis_path) if analysis_path else None,
            "proposal": str(proposal_path) if proposal_path else None,
        },
    }
