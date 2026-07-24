from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from garminconnect import Garmin

from analytics import build_training_analysis, save_analysis
from coach import run_weekly_review
from feedback import feedback_format_help, feedback_summary, fetch_recent_feedback
from planner import AdaptivePlanner, load_plan_config

LOG = logging.getLogger("garmin-adaptive-triathlon-advisor")


@dataclass
class Metrics:
    day: str
    sleep_hours: float | None
    sleep_score: float | None
    hrv_last_night: float | None
    hrv_weekly_average: float | None
    hrv_ratio: float | None
    hrv_status: str | None
    resting_hr: float | None
    resting_hr_baseline: float | None
    resting_hr_delta: float | None
    training_readiness: float | None
    training_readiness_level: str | None
    subjective_feedback: dict[str, Any]


@dataclass
class Advice:
    status: str
    recommendation: str
    reasons: list[str]
    warnings: int


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw is None else float(raw)


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def get_path(obj: Any, *paths: tuple[str, ...]) -> Any:
    for path in paths:
        current = obj
        valid = True
        for key in path:
            if isinstance(current, list):
                if not current:
                    valid = False
                    break
                current = current[0]
            if not isinstance(current, dict) or key not in current:
                valid = False
                break
            current = current[key]
        if valid and current is not None:
            return current
    return None


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_client(interactive: bool = False) -> Garmin:
    token_dir = Path(os.getenv("TOKEN_DIR", "/tokens"))
    token_dir.mkdir(parents=True, exist_ok=True)
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")

    def prompt_mfa() -> str:
        if not interactive:
            raise RuntimeError(
                "Garmin requested MFA. Run the interactive login command again."
            )
        return input("Garmin MFA code: ").strip()

    client = Garmin(email, password, prompt_mfa=prompt_mfa)
    client.login(str(token_dir))
    return client


def safe_call(label: str, function: Any, *args: Any) -> Any:
    try:
        return function(*args)
    except Exception as exc:
        LOG.warning("Could not read %s: %s", label, exc)
        return None


def extract_sleep(payload: Any) -> tuple[float | None, float | None]:
    if not isinstance(payload, dict):
        return None, None
    seconds = number(
        get_path(
            payload,
            ("dailySleepDTO", "sleepTimeSeconds"),
            ("dailySleepDTO", "durationInSeconds"),
            ("sleepTimeSeconds",),
        )
    )
    score = number(
        get_path(
            payload,
            ("dailySleepDTO", "sleepScores", "overall", "value"),
            ("dailySleepDTO", "sleepScore"),
            ("sleepScore",),
        )
    )
    return (round(seconds / 3600, 2) if seconds is not None else None, score)


def extract_hrv(payload: Any) -> tuple[
    float | None, float | None, float | None, str | None
]:
    payload = payload if isinstance(payload, dict) else {}
    last_night = number(
        get_path(
            payload,
            ("hrvSummary", "lastNightAvg"),
            ("hrvSummary", "lastNightAverage"),
            ("lastNightAvg",),
        )
    )
    weekly = number(
        get_path(
            payload,
            ("hrvSummary", "weeklyAvg"),
            ("hrvSummary", "weeklyAverage"),
            ("weeklyAvg",),
        )
    )
    status = get_path(payload, ("hrvSummary", "status"), ("status",))
    ratio = (
        round(last_night / weekly, 3)
        if last_night is not None and weekly not in (None, 0)
        else None
    )
    return last_night, weekly, ratio, str(status) if status is not None else None


def extract_readiness(payload: Any) -> tuple[float | None, str | None]:
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    if not isinstance(payload, dict):
        return None, None
    score = number(get_path(payload, ("score",), ("trainingReadinessScore",)))
    level = get_path(payload, ("level",), ("scoreLevel",))
    return score, str(level) if level is not None else None


def extract_rhr(payload: Any) -> float | None:
    if not isinstance(payload, dict):
        return None
    direct = number(
        get_path(payload, ("restingHeartRate",), ("restingHeartRateValue",))
    )
    if direct is not None:
        return direct
    values = get_path(
        payload, ("allMetrics", "metricsMap", "WELLNESS_RESTING_HEART_RATE")
    )
    if isinstance(values, list) and values:
        return number(values[0].get("value"))
    return None


def fetch_metrics(client: Garmin, today: date, data_dir: Path) -> Metrics:
    day = today.isoformat()
    sleep_payload = safe_call("sleep", client.get_sleep_data, day)
    hrv_payload = safe_call("HRV", client.get_hrv_data, day)
    readiness_method = getattr(
        client,
        "get_morning_training_readiness",
        getattr(client, "get_training_readiness", None),
    )
    readiness_payload = (
        safe_call("training readiness", readiness_method, day)
        if readiness_method
        else None
    )
    heart_payload = safe_call("heart rate", client.get_heart_rates, day)

    sleep_hours, sleep_score = extract_sleep(sleep_payload)
    hrv_last, hrv_weekly, hrv_ratio, hrv_status = extract_hrv(hrv_payload)
    readiness, readiness_level = extract_readiness(readiness_payload)
    resting_hr = extract_rhr(heart_payload)

    past_rhr: list[float] = []
    for offset in range(1, 8):
        prior = (today - timedelta(days=offset)).isoformat()
        value = extract_rhr(safe_call(f"resting HR {prior}", client.get_heart_rates, prior))
        if value is not None:
            past_rhr.append(value)

    baseline = round(sum(past_rhr) / len(past_rhr), 1) if len(past_rhr) >= 3 else None
    delta = (
        round(resting_hr - baseline, 1)
        if resting_hr is not None and baseline is not None
        else None
    )
    recent_feedback = fetch_recent_feedback(
        client,
        today,
        data_dir,
        days=int(os.getenv("SUBJECTIVE_FEEDBACK_DAYS", "4")),
    )
    subjective = feedback_summary(recent_feedback)
    return Metrics(
        day=day,
        sleep_hours=sleep_hours,
        sleep_score=sleep_score,
        hrv_last_night=hrv_last,
        hrv_weekly_average=hrv_weekly,
        hrv_ratio=hrv_ratio,
        hrv_status=hrv_status,
        resting_hr=resting_hr,
        resting_hr_baseline=baseline,
        resting_hr_delta=delta,
        training_readiness=readiness,
        training_readiness_level=readiness_level,
        subjective_feedback=subjective,
    )


def decide(metrics: Metrics) -> Advice:
    thresholds = {
        "readiness_red": env_float("READINESS_RED", 25),
        "readiness_amber": env_float("READINESS_AMBER", 50),
        "sleep_hours_red": env_float("SLEEP_RED_HOURS", 5.0),
        "sleep_hours_amber": env_float("SLEEP_AMBER_HOURS", 6.5),
        "sleep_score_red": env_float("SLEEP_SCORE_RED", 50),
        "sleep_score_amber": env_float("SLEEP_SCORE_AMBER", 70),
        "hrv_red": env_float("HRV_RED_RATIO", 0.80),
        "hrv_amber": env_float("HRV_AMBER_RATIO", 0.90),
        "rhr_red": env_float("RHR_RED_DELTA", 5),
        "rhr_amber": env_float("RHR_AMBER_DELTA", 3),
    }
    red: list[str] = []
    amber: list[str] = []

    def classify(value: float | None, red_limit: float, amber_limit: float, text: str) -> None:
        if value is None:
            return
        if value < red_limit:
            red.append(text.format(value=value, limit=red_limit))
        elif value < amber_limit:
            amber.append(text.format(value=value, limit=amber_limit))

    classify(
        metrics.training_readiness,
        thresholds["readiness_red"],
        thresholds["readiness_amber"],
        "training readiness {value:.0f} is below {limit:.0f}",
    )
    classify(
        metrics.sleep_hours,
        thresholds["sleep_hours_red"],
        thresholds["sleep_hours_amber"],
        "sleep {value:.1f} h is below {limit:.1f} h",
    )
    classify(
        metrics.sleep_score,
        thresholds["sleep_score_red"],
        thresholds["sleep_score_amber"],
        "sleep score {value:.0f} is below {limit:.0f}",
    )
    classify(
        metrics.hrv_ratio,
        thresholds["hrv_red"],
        thresholds["hrv_amber"],
        "overnight HRV is {value:.0%} of the weekly average, below {limit:.0%}",
    )

    if metrics.resting_hr_delta is not None:
        if metrics.resting_hr_delta >= thresholds["rhr_red"]:
            red.append(
                f"resting HR is {metrics.resting_hr_delta:.0f} bpm above baseline"
            )
        elif metrics.resting_hr_delta >= thresholds["rhr_amber"]:
            amber.append(
                f"resting HR is {metrics.resting_hr_delta:.0f} bpm above baseline"
            )

    subjective = metrics.subjective_feedback or {}
    if subjective.get("pain_or_injury_reported") is True:
        red.append("pain or injury was reported in a recent Garmin activity description")
    soreness = number(subjective.get("max_soreness"))
    fatigue = number(subjective.get("max_fatigue"))
    motivation = number(subjective.get("min_motivation"))
    if soreness is not None:
        if soreness >= env_float("SORENESS_RED", 8):
            red.append(f"reported soreness is {soreness:.0f}/10")
        elif soreness >= env_float("SORENESS_AMBER", 6):
            amber.append(f"reported soreness is {soreness:.0f}/10")
    if fatigue is not None:
        if fatigue >= env_float("FATIGUE_RED", 8):
            red.append(f"reported fatigue is {fatigue:.0f}/10")
        elif fatigue >= env_float("FATIGUE_AMBER", 6):
            amber.append(f"reported fatigue is {fatigue:.0f}/10")
    if motivation is not None:
        if motivation <= env_float("MOTIVATION_RED", 2):
            red.append(f"reported motivation is {motivation:.0f}/10")
        elif motivation <= env_float("MOTIVATION_AMBER", 4):
            amber.append(f"reported motivation is {motivation:.0f}/10")

    warnings = len(red) + len(amber)
    if red or warnings >= 3:
        return Advice(
            "RED",
            "Replace training with rest or one short recovery/technique session.",
            red + amber,
            warnings,
        )
    if amber:
        return Advice(
            "AMBER",
            "Remove hard work and reduce the main session by about 25-30%.",
            amber,
            warnings,
        )

    known = sum(
        value is not None
        for value in (
            metrics.training_readiness,
            metrics.sleep_hours,
            metrics.sleep_score,
            metrics.hrv_ratio,
            metrics.resting_hr_delta,
        )
    )
    if known < 2:
        return Advice(
            "UNKNOWN",
            "Not enough recovery data is available; keep the plan conservative.",
            ["fewer than two recovery signals were available"],
            0,
        )
    return Advice(
        "GREEN",
        "Keep the planned session, provided you feel normal and have no pain or illness symptoms.",
        ["recovery signals are within the configured limits"],
        0,
    )


def planner_from_env(client: Garmin) -> AdaptivePlanner:
    config_path = os.getenv("PLAN_CONFIG", "/config/plan.yaml")
    data_dir = Path(os.getenv("DATA_DIR", "/data"))
    config = load_plan_config(config_path)
    return AdaptivePlanner(
        client=client,
        config=config,
        data_dir=data_dir,
        write_enabled=env_bool("GARMIN_WRITE_ENABLED", False),
        dry_run=env_bool("DRY_RUN", True),
    )


def write_report(
        metrics: Metrics,
        advice: Advice,
        plan_result: dict[str, Any],
        adaptation: dict[str, Any],
        coach_review: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    report_dir = Path(os.getenv("DATA_DIR", "/data")) / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "metrics": asdict(metrics),
        "advice": asdict(advice),
        "plan": plan_result,
        "adaptation": adaptation,
        "coach_review": coach_review,
    }
    json_path = report_dir / f"{metrics.day}.json"
    md_path = report_dir / f"{metrics.day}.md"
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    reasons = "\n".join(f"- {reason}" for reason in advice.reasons)
    today_sessions = [
        item
        for item in plan_result.get("planned_sessions", [])
        if item.get("day") == metrics.day
    ]
    session_lines = (
            "\n".join(
                f"- {item['name']} — {item['sport']}, {item['duration_minutes']} min, {item['intensity']}"
                for item in today_sessions
            )
            or "- No managed session is planned today."
    )
    md = f"""# Adaptive triathlon + strength report — {metrics.day}

## Recovery: {advice.status}

{advice.recommendation}

### Why

{reasons}

### Recovery metrics

- Sleep: {metrics.sleep_hours} hours
- Sleep score: {metrics.sleep_score}
- Training readiness: {metrics.training_readiness} ({metrics.training_readiness_level})
- HRV last night: {metrics.hrv_last_night} ms
- HRV weekly average: {metrics.hrv_weekly_average} ms
- HRV ratio: {metrics.hrv_ratio}
- Resting HR: {metrics.resting_hr} bpm
- Seven-day RHR baseline: {metrics.resting_hr_baseline} bpm
- RHR difference: {metrics.resting_hr_delta} bpm

### Subjective feedback from Garmin activity descriptions

```json
{json.dumps(metrics.subjective_feedback, indent=2, default=str)}
```

Use this format in a completed Garmin activity's private description:

`#coach soreness=3 fatigue=4 motivation=8 pain=no rpe=6`

### Today's generated sessions

{session_lines}

### Calendar adaptation

```json
{json.dumps(adaptation, indent=2, default=str)}
```

### Rolling-plan actions

```json
{json.dumps(plan_result.get('actions', []), indent=2, default=str)}
```

### Weekly coach review

```json
{json.dumps(coach_review, indent=2, default=str)}
```

> Recovery scores are guardrails, not a diagnosis. Pain, illness, dizziness,
> unusual breathlessness, or persistent fatigue overrides the automatic plan.
"""
    md_path.write_text(md, encoding="utf-8")
    return json_path, md_path


def _review_day_matches(today: date) -> bool:
    configured = os.getenv("AI_REVIEW_DAY", "sunday").strip().lower()
    return configured in {"daily", "everyday"} or today.strftime("%A").lower() == configured


def run_once() -> int:
    tz = ZoneInfo(os.getenv("TZ", "Pacific/Auckland"))
    today = datetime.now(tz).date()
    data_dir = Path(os.getenv("DATA_DIR", "/data"))
    client = build_client(False)
    config = load_plan_config(os.getenv("PLAN_CONFIG", "/config/plan.yaml"))

    coach_review: dict[str, Any] | None = None
    if _review_day_matches(today):
        coach_review = run_weekly_review(
            client, today, data_dir, config, force=False
        )

    metrics = fetch_metrics(client, today, data_dir)
    advice = decide(metrics)
    planner = AdaptivePlanner(
        client=client,
        config=config,
        data_dir=data_dir,
        write_enabled=env_bool("GARMIN_WRITE_ENABLED", False),
        dry_run=env_bool("DRY_RUN", True),
    )
    plan_result = planner.ensure_horizon(today)
    adaptation = planner.adapt_today(today, advice.status, advice.reasons)
    json_path, md_path = write_report(
        metrics, advice, plan_result, adaptation, coach_review
    )
    print(
        json.dumps(
            {
                "recovery_status": advice.status,
                "recommendation": advice.recommendation,
                "subjective_feedback": metrics.subjective_feedback,
                "coach_review": coach_review,
                "adaptation": adaptation,
                "plan_actions": plan_result.get("actions", []),
                "report": str(md_path),
                "raw_report": str(json_path),
            },
            indent=2,
            default=str,
        )
    )
    return 0


def run_plan(rebuild: bool = False) -> int:
    tz = ZoneInfo(os.getenv("TZ", "Pacific/Auckland"))
    today = datetime.now(tz).date()
    client = build_client(False)
    planner = planner_from_env(client)
    result = planner.rebuild(today) if rebuild else planner.ensure_horizon(today)
    print(json.dumps(result, indent=2, default=str))
    return 0


def run_status() -> int:
    tz = ZoneInfo(os.getenv("TZ", "Pacific/Auckland"))
    today = datetime.now(tz).date()
    client = build_client(False)
    planner = planner_from_env(client)
    print(json.dumps(planner.status(today), indent=2, default=str))
    return 0


def run_analysis() -> int:
    tz = ZoneInfo(os.getenv("TZ", "Pacific/Auckland"))
    today = datetime.now(tz).date()
    data_dir = Path(os.getenv("DATA_DIR", "/data"))
    client = build_client(False)
    analysis = build_training_analysis(
        client, today, data_dir, days=int(os.getenv("ANALYSIS_DAYS", "42"))
    )
    path = save_analysis(analysis, data_dir)
    print(json.dumps({"analysis": asdict(analysis), "file": str(path)}, indent=2, default=str))
    return 0


def run_coach_review(force: bool = True) -> int:
    tz = ZoneInfo(os.getenv("TZ", "Pacific/Auckland"))
    today = datetime.now(tz).date()
    data_dir = Path(os.getenv("DATA_DIR", "/data"))
    client = build_client(False)
    config = load_plan_config(os.getenv("PLAN_CONFIG", "/config/plan.yaml"))
    result = run_weekly_review(client, today, data_dir, config, force=force)
    print(json.dumps(result, indent=2, default=str))
    return 0


def run_ai_check() -> int:
    from local_ai import ram_check_payload

    payload = ram_check_payload()
    print(json.dumps(payload, indent=2, default=str))
    return 0 if payload.get("enough_ram") else 2


def run_daemon() -> int:
    tz_name = os.getenv("TZ", "Pacific/Auckland")
    hour = int(os.getenv("RUN_HOUR", "6"))
    minute = int(os.getenv("RUN_MINUTE", "30"))
    scheduler = BlockingScheduler(timezone=ZoneInfo(tz_name))
    scheduler.add_job(
        run_once,
        "cron",
        hour=hour,
        minute=minute,
        id="adaptive-triathlon-morning-run",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60 * 60 * 3,
    )
    LOG.info("Scheduled daily adaptive run at %02d:%02d in %s", hour, minute, tz_name)
    scheduler.start()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=(
            "login",
            "run",
            "plan",
            "rebuild",
            "status",
            "analyze",
            "coach",
            "feedback-help",
            "ai-check",
            "daemon",
        ),
    )
    args = parser.parse_args()
    if args.command == "login":
        build_client(True)
        print("Garmin login succeeded and tokens were saved.")
        return 0
    if args.command == "run":
        return run_once()
    if args.command == "plan":
        return run_plan(False)
    if args.command == "rebuild":
        return run_plan(True)
    if args.command == "status":
        return run_status()
    if args.command == "analyze":
        return run_analysis()
    if args.command == "coach":
        return run_coach_review(True)
    if args.command == "feedback-help":
        print(feedback_format_help())
        return 0
    if args.command == "ai-check":
        return run_ai_check()
    return run_daemon()


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        LOG.exception("Advisor failed: %s", exc)
        raise SystemExit(1)
