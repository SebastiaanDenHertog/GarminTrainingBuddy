from __future__ import annotations

import copy
import json
import logging
import os
import re
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from analytics import TrainingAnalysis, build_training_analysis, save_analysis

LOG = logging.getLogger("garmin-ai-coach")

ALLOWED_DECISIONS = {"hold", "progress", "reduce", "change_stimulus"}
ALLOWED_QUALITY_ACTIONS = {"maintain", "add_interval", "remove_interval", "rotate"}
SPORTS = {"run", "bike", "swim"}
PLANNED_SPORTS = {"run", "bike", "swim", "strength"}


@dataclass
class CoachProposal:
    proposal_id: str
    generated_at: str
    source: str
    decision: str
    confidence: float
    weekly_volume_change_percent: float
    sport_volume_change_percent: dict[str, float]
    quality_changes: list[dict[str, Any]]
    reasoning_summary: list[str]
    evidence_ids: list[str]
    valid_for_weeks: int


@dataclass
class ValidationResult:
    accepted: bool
    errors: list[str]
    warnings: list[str]
    normalized_proposal: dict[str, Any]


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_evidence(path: str | Path) -> dict[str, dict[str, Any]]:
    evidence_path = Path(path)
    if not evidence_path.exists():
        raise RuntimeError(f"Evidence file does not exist: {evidence_path}")
    payload = yaml.safe_load(evidence_path.read_text(encoding="utf-8")) or {}
    entries = payload.get("evidence") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise RuntimeError("Evidence YAML must contain an 'evidence' list.")
    result: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if isinstance(entry, dict) and entry.get("id"):
            result[str(entry["id"])] = entry
    return result


def compact_analysis(analysis: TrainingAnalysis) -> dict[str, Any]:
    return {
        "period": f"{analysis.period_start}/{analysis.period_end}",
        "activity_count": analysis.activity_count,
        "total_minutes": analysis.total_minutes,
        "recent_14d_minutes": analysis.recent_14d_minutes,
        "previous_14d_minutes": analysis.previous_14d_minutes,
        "volume_change_percent": analysis.volume_change_percent,
        "compliance": analysis.compliance,
        "completion_by_sport": analysis.completion_by_sport,
        "efficiency_change_percent": analysis.efficiency_change_percent,
        "hard_sessions_recent_7d": analysis.hard_sessions_recent_7d,
        "pain_reported": analysis.pain_reported,
        "high_soreness_reported": analysis.high_soreness_reported,
        "high_fatigue_reported": analysis.high_fatigue_reported,
        "low_motivation_reported": analysis.low_motivation_reported,
        "feedback_entries": analysis.feedback_entries,
        "average_rpe": analysis.average_rpe,
        "recovery_red_days": analysis.recovery_red_days,
        "recovery_amber_days": analysis.recovery_amber_days,
        "average_training_readiness": analysis.average_training_readiness,
        "average_sleep_score": analysis.average_sleep_score,
        "average_hrv_ratio": analysis.average_hrv_ratio,
        "current_week_index": analysis.current_week_index,
        "recovery_week": analysis.recovery_week,
        "structured_training_start_date": analysis.structured_training_start_date,
        "training_age_weeks": analysis.training_age_weeks,
        "training_experience_band": analysis.training_experience_band,
        "training_age_warning": analysis.training_age_warning,
    }


def _new_proposal(
        source: str,
        decision: str,
        confidence: float,
        weekly_change: float,
        sport_changes: dict[str, float],
        quality_changes: list[dict[str, Any]],
        reasons: list[str],
        evidence: list[str],
        valid_for_weeks: int = 1,
) -> CoachProposal:
    return CoachProposal(
        proposal_id=f"coach-{date.today().isoformat()}-{uuid.uuid4().hex[:8]}",
        generated_at=datetime.now().astimezone().isoformat(),
        source=source,
        decision=decision,
        confidence=round(max(0.0, min(1.0, confidence)), 3),
        weekly_volume_change_percent=round(weekly_change, 2),
        sport_volume_change_percent={
            sport: round(float(sport_changes.get(sport, 0)), 2) for sport in SPORTS
        },
        quality_changes=quality_changes,
        reasoning_summary=reasons[:6],
        evidence_ids=evidence,
        valid_for_weeks=max(1, min(4, int(valid_for_weeks))),
    )


def deterministic_proposal(
        analysis: TrainingAnalysis,
        config: dict[str, Any],
) -> CoachProposal:
    max_hard = int(config.get("plan", {}).get("max_hard_sessions_per_week", 2))
    efficiency = analysis.efficiency_change_percent
    known_changes = [value for value in efficiency.values() if value is not None]

    if analysis.pain_reported:
        return _new_proposal(
            "deterministic",
            "reduce",
            0.99,
            -10,
            {"run": -15, "bike": -8, "swim": -5},
            [
                {"sport": "run", "action": "remove_interval", "amount": 1},
                {"sport": "bike", "action": "remove_interval", "amount": 1},
            ],
            ["Pain or injury was reported in a Garmin activity description."],
            ["SAFETY_PAIN_001", "LOAD_REDUCE_001"],
        )

    if (
            analysis.high_soreness_reported
            or analysis.high_fatigue_reported
            or analysis.low_motivation_reported
            or analysis.recovery_red_days >= 2
    ):
        return _new_proposal(
            "deterministic",
            "reduce",
            0.9,
            -6,
            {"run": -8, "bike": -5, "swim": 0},
            [{"sport": "run", "action": "remove_interval", "amount": 1}],
            [
                "Recent subjective feedback or recovery history indicates accumulated fatigue.",
                "The change is temporary and preserves training frequency where possible.",
            ],
            ["RECOVERY_TREND_001", "LOAD_REDUCE_001"],
        )

    if analysis.compliance is not None and analysis.compliance < 0.70:
        return _new_proposal(
            "deterministic",
            "reduce",
            0.86,
            -5,
            {"run": -5, "bike": -5, "swim": -5},
            [],
            [
                "Less than 70% of planner sessions were matched to completed Garmin activities.",
                "The plan should become achievable before becoming harder.",
            ],
            ["COMPLIANCE_001", "LOAD_REDUCE_001"],
        )

    if analysis.recovery_week:
        return _new_proposal(
            "deterministic",
            "hold",
            0.98,
            0,
            {},
            [],
            ["This is the configured recovery week, so progression is deferred."],
            ["RECOVERY_WEEK_001"],
        )

    if analysis.activity_count < 8 or analysis.compliance is None:
        return _new_proposal(
            "deterministic",
            "hold",
            0.8,
            0,
            {},
            [],
            ["There is not enough matched activity history to justify progression."],
            ["DATA_QUALITY_001"],
        )

    stable_recovery = (
            analysis.recovery_red_days == 0
            and not analysis.high_soreness_reported
            and not analysis.high_fatigue_reported
    )
    early_foundation = (
            analysis.training_age_weeks is not None
            and analysis.training_age_weeks < 8
    )
    high_compliance = analysis.compliance >= 0.85
    improving = any(value >= 1.5 for value in known_changes)
    material_decline = any(value <= -2.5 for value in known_changes)

    if high_compliance and stable_recovery and improving and not material_decline:
        weakest = min(
            (
                (sport, value)
                for sport, value in efficiency.items()
                if value is not None
            ),
            key=lambda item: item[1],
            default=("bike", 0.0),
        )[0]
        progress_percent = 2 if early_foundation else 3
        reasons = [
            "Training compliance is high and recent recovery is stable.",
            "At least one sport-specific efficiency trend is improving.",
            "Only a small volume increase is proposed.",
        ]
        evidence_ids = [
            "PROGRESS_SMALL_001",
            "ONE_VARIABLE_001",
            "COMPLIANCE_001",
        ]
        if early_foundation:
            reasons.append(
                "Structured training age is under eight weeks, so progression "
                "is capped at two percent and intensity is not increased."
            )
            evidence_ids.append("TRAINING_AGE_001")
        return _new_proposal(
            "deterministic",
            "progress",
            0.84,
            progress_percent,
            {weakest: progress_percent},
            [],
            reasons,
            evidence_ids,
        )

    plateau = bool(known_changes) and all(abs(value) < 1.25 for value in known_changes)
    if high_compliance and stable_recovery and plateau and early_foundation:
        return _new_proposal(
            "deterministic",
            "hold",
            0.9,
            0,
            {},
            [],
            [
                "Performance is approximately flat, but structured training "
                "age is still under eight weeks.",
                "Build consistency before adding another intensity stimulus.",
            ],
            ["TRAINING_AGE_001", "HOLD_WHEN_UNCERTAIN_001"],
        )

    if (
            high_compliance
            and stable_recovery
            and plateau
            and analysis.hard_sessions_recent_7d < max_hard
    ):
        target_sport = min(
            (
                (sport, value)
                for sport, value in efficiency.items()
                if value is not None
            ),
            key=lambda item: item[1],
            default=("bike", 0.0),
        )[0]
        return _new_proposal(
            "deterministic",
            "change_stimulus",
            0.78,
            0,
            {},
            [{"sport": target_sport, "action": "add_interval", "amount": 1}],
            [
                "Performance is approximately flat despite high compliance.",
                "Recovery is stable and the recent hard-session count is below the configured limit.",
                "Volume is held while one quality stimulus is progressed.",
            ],
            ["PLATEAU_001", "ONE_VARIABLE_001", "HARD_SESSION_LIMIT_001"],
        )

    return _new_proposal(
        "deterministic",
        "hold",
        0.82,
        0,
        {},
        [],
        [
            "The current plan is either working or the available trends are mixed.",
            "No change is safer than changing volume and intensity without a clear signal.",
        ],
        ["HOLD_WHEN_UNCERTAIN_001"],
    )


def _extract_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.I)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        value = json.loads(stripped)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        value = json.loads(stripped[start: end + 1])
        if isinstance(value, dict):
            return value
    raise ValueError("AI response did not contain a JSON object.")


def _ai_prompt(
        analysis: TrainingAnalysis,
        deterministic: CoachProposal,
        evidence: dict[str, dict[str, Any]],
) -> tuple[str, str]:
    evidence_catalog = [
        {
            "id": key,
            "rule": value.get("rule"),
            "allowed_decisions": value.get("allowed_decisions", []),
        }
        for key, value in evidence.items()
    ]
    schema = {
        "decision": "hold|progress|reduce|change_stimulus",
        "confidence": "number 0..1",
        "weekly_volume_change_percent": "number -15..5",
        "sport_volume_change_percent": {"run": 0, "bike": 0, "swim": 0},
        "quality_changes": [
            {
                "sport": "run|bike|swim",
                "action": "maintain|add_interval|remove_interval|rotate",
                "amount": "integer 0..2",
            }
        ],
        "reasoning_summary": ["short factual reason"],
        "evidence_ids": ["ID_FROM_CATALOG"],
        "valid_for_weeks": "integer 1..4",
    }
    system = (
        "You are a conservative triathlon planning component. You may propose, "
        "but Python safety rules decide whether the proposal is accepted. Use only "
        "the supplied facts. Never invent measurements. Progress at most one main "
        "training variable. Return JSON only, with exactly the requested fields."
    )
    user = {
        "analysis": compact_analysis(analysis),
        "deterministic_reference": asdict(deterministic),
        "evidence_catalog": evidence_catalog,
        "output_schema": schema,
    }
    return system, json.dumps(user, separators=(",", ":"))


def _proposal_from_ai_content(content: str) -> CoachProposal:
    raw = _extract_json(content)
    return _new_proposal(
        "local_ai",
        str(raw.get("decision", "hold")),
        _number(raw.get("confidence"), 0.0),
        _number(raw.get("weekly_volume_change_percent"), 0.0),
        raw.get("sport_volume_change_percent")
        if isinstance(raw.get("sport_volume_change_percent"), dict)
        else {},
        raw.get("quality_changes")
        if isinstance(raw.get("quality_changes"), list)
        else [],
        [
            str(item)
            for item in raw.get("reasoning_summary", [])
            if isinstance(item, str)
        ],
        [
            str(item)
            for item in raw.get("evidence_ids", [])
            if isinstance(item, str)
        ],
        int(_number(raw.get("valid_for_weeks"), 1)),
    )


def request_embedded_ai_proposal(
        analysis: TrainingAnalysis,
        deterministic: CoachProposal,
        evidence: dict[str, dict[str, Any]],
) -> CoachProposal:
    from local_ai import generate_chat_completion

    system, user = _ai_prompt(analysis, deterministic, evidence)
    content = generate_chat_completion(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_new_tokens=int(os.getenv("LOCAL_AI_MAX_NEW_TOKENS", "1400")),
        temperature=float(os.getenv("LOCAL_AI_TEMPERATURE", "0")),
    )
    return _proposal_from_ai_content(content)


def request_external_ai_proposal(
        analysis: TrainingAnalysis,
        deterministic: CoachProposal,
        evidence: dict[str, dict[str, Any]],
) -> CoachProposal:
    base_url = os.getenv("AI_BASE_URL", "http://ollama:11434/v1").rstrip("/")
    model = os.getenv("AI_MODEL", "qwen3:30b-a3b")
    api_key = os.getenv("AI_API_KEY", "ollama")
    timeout = int(os.getenv("AI_TIMEOUT_SECONDS", "120"))
    system, user = _ai_prompt(analysis, deterministic, evidence)
    payload = {
        "model": model,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"External local-AI request failed: {exc}") from exc

    content = (((body.get("choices") or [{}])[0].get("message") or {}).get("content"))
    if not isinstance(content, str):
        raise RuntimeError("External local-AI response did not contain message content.")
    return _proposal_from_ai_content(content)


def request_ai_proposal(
        analysis: TrainingAnalysis,
        deterministic: CoachProposal,
        evidence: dict[str, dict[str, Any]],
) -> CoachProposal:
    backend = os.getenv("AI_BACKEND", "embedded").strip().lower()
    if backend == "embedded":
        return request_embedded_ai_proposal(analysis, deterministic, evidence)
    if backend in {"external", "openai", "ollama"}:
        return request_external_ai_proposal(analysis, deterministic, evidence)
    raise RuntimeError(
        f"Unsupported AI_BACKEND={backend!r}; use 'embedded' or 'external'."
    )


def validate_proposal(
        proposal: CoachProposal,
        analysis: TrainingAnalysis,
        config: dict[str, Any],
        evidence: dict[str, dict[str, Any]],
) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    raw = asdict(proposal)

    if proposal.decision not in ALLOWED_DECISIONS:
        errors.append(f"Unknown decision: {proposal.decision}")
    if not 0 <= proposal.confidence <= 1:
        errors.append("confidence must be between 0 and 1")
    if not -15 <= proposal.weekly_volume_change_percent <= 5:
        errors.append("weekly volume change must be between -15% and +5%")

    for sport, value in proposal.sport_volume_change_percent.items():
        if sport not in SPORTS:
            errors.append(f"Unknown sport in volume changes: {sport}")
        if not -20 <= float(value) <= 7:
            errors.append(f"Sport volume change for {sport} is outside -20%..+7%")

    for change in proposal.quality_changes:
        if not isinstance(change, dict):
            errors.append("Every quality change must be an object")
            continue
        sport = str(change.get("sport", ""))
        action = str(change.get("action", ""))
        amount = int(_number(change.get("amount"), 0))
        if sport not in SPORTS:
            errors.append(f"Unknown quality-change sport: {sport}")
        if action not in ALLOWED_QUALITY_ACTIONS:
            errors.append(f"Unknown quality action: {action}")
        if not 0 <= amount <= 2:
            errors.append("Quality change amount must be 0, 1, or 2")

    unknown_evidence = [item for item in proposal.evidence_ids if item not in evidence]
    if unknown_evidence:
        errors.append(f"Unknown evidence IDs: {', '.join(unknown_evidence)}")
    if not proposal.evidence_ids:
        errors.append("At least one evidence ID is required")
    for evidence_id in proposal.evidence_ids:
        allowed = evidence.get(evidence_id, {}).get("allowed_decisions", [])
        if allowed and proposal.decision not in allowed:
            errors.append(
                f"Evidence {evidence_id} does not allow decision {proposal.decision}"
            )

    positive_volume = proposal.weekly_volume_change_percent > 0 or any(
        value > 0 for value in proposal.sport_volume_change_percent.values()
    )
    adds_intensity = any(
        str(item.get("action")) == "add_interval" and int(_number(item.get("amount"), 0)) > 0
        for item in proposal.quality_changes
        if isinstance(item, dict)
    )
    if positive_volume or adds_intensity or proposal.decision in {"progress", "change_stimulus"}:
        if analysis.pain_reported:
            errors.append("Progression is blocked because pain/injury was reported")
        if analysis.recovery_week:
            errors.append("Progression is blocked during a recovery week")
        if analysis.recovery_red_days >= 2:
            errors.append("Progression is blocked after two or more RED recovery days")
        if analysis.high_soreness_reported or analysis.high_fatigue_reported:
            errors.append("Progression is blocked by high soreness or fatigue feedback")
        if analysis.compliance is None or analysis.compliance < 0.80:
            errors.append("Progression requires at least 80% matched-session compliance")

    if analysis.training_age_weeks is not None and analysis.training_age_weeks < 8:
        if proposal.weekly_volume_change_percent > 2:
            errors.append(
                "During the first eight structured-training weeks, weekly "
                "progression is capped at 2%"
            )
        if any(value > 2 for value in proposal.sport_volume_change_percent.values()):
            errors.append(
                "During the first eight structured-training weeks, sport "
                "volume progression is capped at 2%"
            )
        if adds_intensity or proposal.decision == "change_stimulus":
            errors.append(
                "Interval progression is blocked during the first eight "
                "structured-training weeks"
            )

    max_hard = int(config.get("plan", {}).get("max_hard_sessions_per_week", 2))
    if adds_intensity and analysis.hard_sessions_recent_7d >= max_hard:
        errors.append("An interval increase would exceed the configured hard-session limit")

    if positive_volume and adds_intensity:
        errors.append("Volume and intensity may not both increase in the same proposal")
    if proposal.decision == "hold" and (
            proposal.weekly_volume_change_percent != 0
            or any(value != 0 for value in proposal.sport_volume_change_percent.values())
            or proposal.quality_changes
    ):
        errors.append("A hold proposal may not change volume or quality")
    if proposal.decision == "reduce" and proposal.weekly_volume_change_percent > 0:
        errors.append("A reduce proposal may not increase weekly volume")
    if proposal.confidence < 0.65:
        errors.append("Proposal confidence is below the 0.65 acceptance threshold")
    if analysis.feedback_entries == 0:
        warnings.append(
            "No Garmin activity-description feedback was found; subjective checks are missing."
        )
    if analysis.activity_count < 8:
        warnings.append("Fewer than eight activities were available for trend analysis.")
    if analysis.training_age_warning:
        warnings.append(analysis.training_age_warning)
    elif analysis.training_age_weeks is None:
        warnings.append(
            "STRUCTURED_TRAINING_START_DATE is not configured; training age is not used."
        )
    elif analysis.training_age_weeks < 8:
        warnings.append(
            "The athlete is in the first eight structured-training weeks; "
            "progression is intentionally conservative."
        )

    return ValidationResult(
        accepted=not errors,
        errors=errors,
        warnings=warnings,
        normalized_proposal=raw,
    )


def _default_adjustment() -> dict[str, Any]:
    return {
        "version": 1,
        "weekly_volume_multiplier": 1.0,
        "sport_weight_multipliers": {"run": 1.0, "bike": 1.0, "swim": 1.0, "strength": 1.0},
        "quality_progression": {"run": 0, "bike": 0, "swim": 0},
        "proposal_id": None,
        "valid_until": None,
    }


def load_active_adjustment(data_dir: Path, today: date | None = None) -> dict[str, Any]:
    path = data_dir / "coach" / "active_adjustment.json"
    if not path.exists():
        return _default_adjustment()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _default_adjustment()
    if not isinstance(payload, dict):
        return _default_adjustment()
    result = {**_default_adjustment(), **payload}
    today = today or date.today()
    valid_until = result.get("valid_until")
    if valid_until:
        try:
            if date.fromisoformat(str(valid_until)) < today:
                return _default_adjustment()
        except ValueError:
            return _default_adjustment()
    return result


def apply_adjustment_to_config(
        config: dict[str, Any], data_dir: Path, today: date | None = None
) -> dict[str, Any]:
    result = copy.deepcopy(config)
    adjustment = load_active_adjustment(data_dir, today)
    result.setdefault("plan", {})["coach_volume_multiplier"] = float(
        adjustment.get("weekly_volume_multiplier", 1.0)
    )
    result["plan"]["sport_weight_multipliers"] = {
        sport: float((adjustment.get("sport_weight_multipliers") or {}).get(sport, 1.0))
        for sport in PLANNED_SPORTS
    }
    result["plan"]["quality_progression"] = {
        sport: int((adjustment.get("quality_progression") or {}).get(sport, 0))
        for sport in SPORTS
    }
    result["plan"]["active_coach_proposal_id"] = adjustment.get("proposal_id")
    return result


def _write_active_adjustment(
        proposal: CoachProposal,
        data_dir: Path,
        today: date,
) -> Path:
    current = load_active_adjustment(data_dir, today)
    weekly_multiplier = float(current.get("weekly_volume_multiplier", 1.0))
    weekly_multiplier *= 1 + proposal.weekly_volume_change_percent / 100
    weekly_multiplier = round(max(0.85, min(1.15, weekly_multiplier)), 4)

    sport_multipliers = dict(current.get("sport_weight_multipliers") or {})
    for sport in PLANNED_SPORTS:
        value = float(sport_multipliers.get(sport, 1.0))
        # The AI currently changes endurance sports only. Strength follows the
        # validated weekly multiplier and phase-specific planner rules.
        value *= 1 + proposal.sport_volume_change_percent.get(sport, 0) / 100
        sport_multipliers[sport] = round(max(0.80, min(1.20, value)), 4)

    quality = dict(current.get("quality_progression") or {})
    for sport in SPORTS:
        quality[sport] = int(quality.get(sport, 0))
    for change in proposal.quality_changes:
        sport = str(change.get("sport"))
        action = str(change.get("action"))
        amount = int(_number(change.get("amount"), 0))
        if sport not in SPORTS:
            continue
        if action == "add_interval":
            quality[sport] = min(3, quality[sport] + amount)
        elif action == "remove_interval":
            quality[sport] = max(-2, quality[sport] - amount)
        elif action == "rotate":
            quality[sport] = 0

    valid_until = today + timedelta(weeks=proposal.valid_for_weeks)
    payload = {
        "version": 1,
        "updated_at": datetime.now().astimezone().isoformat(),
        "proposal_id": proposal.proposal_id,
        "decision": proposal.decision,
        "weekly_volume_multiplier": weekly_multiplier,
        "sport_weight_multipliers": sport_multipliers,
        "quality_progression": quality,
        "valid_until": valid_until.isoformat(),
    }
    path = data_dir / "coach" / "active_adjustment.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temp.replace(path)
    return path


def _proposal_dir(data_dir: Path) -> Path:
    path = data_dir / "coach" / "proposals"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _week_key(today: date) -> str:
    monday = today - timedelta(days=today.weekday())
    return monday.isoformat()


def _existing_weekly_proposal(data_dir: Path, today: date) -> Path | None:
    prefix = _week_key(today)
    paths = sorted(_proposal_dir(data_dir).glob(f"{prefix}-*.json"))
    return paths[-1] if paths else None


def evaluate_outcomes(data_dir: Path, analysis: TrainingAnalysis, today: date) -> list[dict[str, Any]]:
    evaluated: list[dict[str, Any]] = []
    for path in _proposal_dir(data_dir).glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not payload.get("applied") or payload.get("outcome"):
            continue
        generated = str(payload.get("proposal", {}).get("generated_at", ""))
        try:
            generated_day = datetime.fromisoformat(generated).date()
        except ValueError:
            continue
        valid_weeks = int(payload.get("proposal", {}).get("valid_for_weeks", 1))
        if today < generated_day + timedelta(weeks=max(2, valid_weeks)):
            continue
        baseline = payload.get("baseline") or {}
        baseline_compliance = baseline.get("compliance")
        current_compliance = analysis.compliance
        baseline_red = int(baseline.get("recovery_red_days") or 0)
        efficiency_now = [
            value for value in analysis.efficiency_change_percent.values() if value is not None
        ]
        positive_signal = any(value >= 1.0 for value in efficiency_now)
        negative_signal = any(value <= -3.0 for value in efficiency_now)
        compliance_drop = (
                baseline_compliance is not None
                and current_compliance is not None
                and current_compliance < float(baseline_compliance) - 0.12
        )
        if analysis.pain_reported or compliance_drop or analysis.recovery_red_days > baseline_red + 1 or negative_signal:
            label = "negative"
        elif positive_signal and not compliance_drop and not analysis.pain_reported:
            label = "positive"
        else:
            label = "inconclusive"
        outcome = {
            "evaluated_at": datetime.now().astimezone().isoformat(),
            "label": label,
            "current": compact_analysis(analysis),
            "method": "deterministic two-or-more-week outcome check",
        }
        payload["outcome"] = outcome
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        evaluated.append({"proposal_file": str(path), **outcome})
    return evaluated


def run_weekly_review(
        client: Any,
        today: date,
        data_dir: Path,
        config: dict[str, Any],
        *,
        force: bool = False,
) -> dict[str, Any]:
    existing = _existing_weekly_proposal(data_dir, today)
    if existing and not force:
        return {
            "status": "already_reviewed_this_week",
            "proposal_file": str(existing),
        }

    analysis = build_training_analysis(
        client,
        today,
        data_dir,
        days=int(os.getenv("ANALYSIS_DAYS", "42")),
    )
    analysis_path = save_analysis(analysis, data_dir)
    evidence_path = os.getenv("EVIDENCE_CONFIG", "/config/evidence.yaml")
    evidence = load_evidence(evidence_path)
    deterministic = deterministic_proposal(analysis, config)
    chosen = deterministic
    ai_error: str | None = None

    ai_enabled = env_bool("AI_ENABLED", False)
    ai_backend = os.getenv("AI_BACKEND", "embedded").strip().lower()
    ai_runtime: dict[str, Any] | None = None
    if ai_enabled and ai_backend == "embedded":
        from local_ai import ram_check_payload

        ai_runtime = ram_check_payload()
    elif ai_enabled:
        ai_runtime = {"backend": ai_backend, "ram_preflight": "not_applicable"}

    if ai_enabled:
        try:
            chosen = request_ai_proposal(analysis, deterministic, evidence)
        except Exception as exc:
            ai_error = str(exc)
            LOG.warning("Using deterministic proposal because local AI failed: %s", exc)

    validation = validate_proposal(chosen, analysis, config, evidence)
    # An invalid AI proposal never gets a second chance to write. The validated
    # deterministic result may be used as a safe fallback in the same report.
    fallback: dict[str, Any] | None = None
    if chosen.source == "local_ai" and not validation.accepted:
        deterministic_validation = validate_proposal(
            deterministic, analysis, config, evidence
        )
        fallback = {
            "proposal": asdict(deterministic),
            "validation": asdict(deterministic_validation),
        }

    shadow = env_bool("AI_SHADOW_MODE", True)
    auto_apply = env_bool("COACH_AUTO_APPLY", False)
    applied = False
    adjustment_path: Path | None = None
    if validation.accepted and auto_apply and not shadow:
        adjustment_path = _write_active_adjustment(chosen, data_dir, today)
        applied = True

    evaluated_outcomes = evaluate_outcomes(data_dir, analysis, today)
    payload = {
        "week_start": _week_key(today),
        "created_at": datetime.now().astimezone().isoformat(),
        "analysis_file": str(analysis_path),
        "baseline": compact_analysis(analysis),
        "deterministic_reference": asdict(deterministic),
        "proposal": asdict(chosen),
        "validation": asdict(validation),
        "fallback": fallback,
        "ai_enabled": ai_enabled,
        "ai_backend": ai_backend,
        "ai_runtime": ai_runtime,
        "ai_error": ai_error,
        "shadow_mode": shadow,
        "auto_apply_enabled": auto_apply,
        "applied": applied,
        "active_adjustment_file": str(adjustment_path) if adjustment_path else None,
        "evaluated_outcomes": evaluated_outcomes,
    }
    filename = f"{_week_key(today)}-{chosen.proposal_id}.json"
    proposal_path = _proposal_dir(data_dir) / filename
    proposal_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return {
        "status": "applied" if applied else "shadow" if shadow else "not_applied",
        "analysis_file": str(analysis_path),
        "proposal_file": str(proposal_path),
        "proposal": asdict(chosen),
        "validation": asdict(validation),
        "fallback": fallback,
        "ai_enabled": ai_enabled,
        "ai_backend": ai_backend,
        "ai_runtime": ai_runtime,
        "ai_error": ai_error,
        "active_adjustment_file": str(adjustment_path) if adjustment_path else None,
        "evaluated_outcomes": evaluated_outcomes,
    }
