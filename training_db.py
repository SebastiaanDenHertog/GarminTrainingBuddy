from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator, Sequence

from matching import MatchResult

SCHEMA_VERSION = 1


def database_path(data_dir: Path | str) -> Path:
    configured = os.getenv("TRAINING_DB_PATH", "").strip()
    return Path(configured) if configured else Path(data_dir) / "training.db"


def database_enabled() -> bool:
    return os.getenv("TRAINING_DB_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@contextmanager
def connect(data_dir: Path | str, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    path = database_path(data_dir)
    if not readonly:
        path.parent.mkdir(parents=True, exist_ok=True)
    if readonly:
        uri = f"file:{path}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=10)
    else:
        connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    if not readonly:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
    try:
        yield connection
        if not readonly:
            connection.commit()
    except Exception:
        if not readonly:
            connection.rollback()
        raise
    finally:
        connection.close()


def initialize(data_dir: Path | str) -> Path:
    path = database_path(data_dir)
    with connect(data_dir) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS planned_sessions (
                session_key TEXT PRIMARY KEY,
                day TEXT NOT NULL,
                sport TEXT NOT NULL,
                role TEXT NOT NULL,
                name TEXT,
                duration_minutes INTEGER NOT NULL,
                scheduled_time TEXT,
                intensity TEXT,
                phase TEXT,
                week_index INTEGER,
                slot_index INTEGER,
                is_primary INTEGER NOT NULL DEFAULT 1,
                benchmark_id TEXT,
                fingerprint TEXT,
                status TEXT,
                workout_id INTEGER,
                scheduled_workout_id INTEGER,
                session_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_planned_day_sport
                ON planned_sessions(day, sport);

            CREATE TABLE IF NOT EXISTS activities (
                activity_id INTEGER PRIMARY KEY,
                day TEXT NOT NULL,
                start_time_local TEXT,
                sport TEXT NOT NULL,
                name TEXT,
                duration_minutes INTEGER NOT NULL,
                distance_m REAL,
                average_hr REAL,
                max_hr REAL,
                average_power REAL,
                normalized_power REAL,
                max_power REAL,
                average_speed_mps REAL,
                aerobic_training_effect REAL,
                anaerobic_training_effect REAL,
                training_load REAL,
                vo2max REAL,
                efficiency_score REAL,
                workout_id INTEGER,
                soreness INTEGER,
                fatigue INTEGER,
                motivation INTEGER,
                pain_or_injury INTEGER,
                session_rpe INTEGER,
                sample_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_activities_day_sport
                ON activities(day, sport);

            CREATE TABLE IF NOT EXISTS session_matches (
                session_key TEXT PRIMARY KEY,
                activity_id INTEGER NOT NULL UNIQUE,
                score REAL NOT NULL,
                method TEXT NOT NULL,
                day_difference INTEGER NOT NULL,
                duration_ratio REAL NOT NULL,
                name_similarity REAL NOT NULL,
                reasons_json TEXT NOT NULL,
                matched_at TEXT NOT NULL,
                FOREIGN KEY(session_key) REFERENCES planned_sessions(session_key)
                    ON DELETE CASCADE,
                FOREIGN KEY(activity_id) REFERENCES activities(activity_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS recovery_daily (
                day TEXT PRIMARY KEY,
                status TEXT,
                training_readiness REAL,
                sleep_hours REAL,
                sleep_score REAL,
                hrv_ratio REAL,
                resting_hr_delta REAL,
                report_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS benchmark_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                benchmark_id TEXT NOT NULL,
                sport TEXT NOT NULL,
                activity_id INTEGER NOT NULL UNIQUE,
                session_key TEXT,
                day TEXT NOT NULL,
                result_value REAL,
                result_unit TEXT,
                secondary_value REAL,
                secondary_unit TEXT,
                confidence REAL NOT NULL,
                method TEXT NOT NULL,
                details_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(activity_id) REFERENCES activities(activity_id)
                    ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_benchmark_sport_day
                ON benchmark_results(sport, day DESC);

            CREATE TABLE IF NOT EXISTS analysis_snapshots (
                generated_at TEXT PRIMARY KEY,
                period_start TEXT NOT NULL,
                period_end TEXT NOT NULL,
                analysis_json TEXT NOT NULL
            );
            """
        )
        db.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
    return path


def _json(value: Any) -> str:
    if is_dataclass(value):
        value = asdict(value)
    return json.dumps(value, sort_keys=True, default=str)


def _bool_int(value: Any) -> int | None:
    if value is None:
        return None
    return 1 if bool(value) else 0


def sync_planner_state(data_dir: Path | str, state: dict[str, Any]) -> int:
    if not database_enabled():
        return 0
    initialize(data_dir)
    now = datetime.now().astimezone().isoformat()
    rows = 0
    with connect(data_dir) as db:
        for record in (state.get("sessions") or {}).values():
            if not isinstance(record, dict):
                continue
            session = record.get("session") or {}
            key = str(session.get("key") or "")
            if not key or not session.get("day") or not session.get("sport"):
                continue
            db.execute(
                """
                INSERT INTO planned_sessions(
                    session_key, day, sport, role, name, duration_minutes,
                    scheduled_time, intensity, phase, week_index, slot_index,
                    is_primary, benchmark_id, fingerprint, status, workout_id,
                    scheduled_workout_id, session_json, first_seen_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(session_key) DO UPDATE SET
                    day=excluded.day,
                    sport=excluded.sport,
                    role=excluded.role,
                    name=excluded.name,
                    duration_minutes=excluded.duration_minutes,
                    scheduled_time=excluded.scheduled_time,
                    intensity=excluded.intensity,
                    phase=excluded.phase,
                    week_index=excluded.week_index,
                    slot_index=excluded.slot_index,
                    is_primary=excluded.is_primary,
                    benchmark_id=excluded.benchmark_id,
                    fingerprint=excluded.fingerprint,
                    status=excluded.status,
                    workout_id=excluded.workout_id,
                    scheduled_workout_id=excluded.scheduled_workout_id,
                    session_json=excluded.session_json,
                    updated_at=excluded.updated_at
                """,
                (
                    key,
                    str(session.get("day")),
                    str(session.get("sport")),
                    str(session.get("role") or "easy"),
                    session.get("name"),
                    int(session.get("duration_minutes") or 0),
                    session.get("scheduled_time"),
                    session.get("intensity"),
                    session.get("phase"),
                    session.get("week_index"),
                    session.get("slot_index"),
                    1 if session.get("primary", True) else 0,
                    session.get("benchmark_id"),
                    record.get("fingerprint"),
                    record.get("status"),
                    record.get("workout_id"),
                    record.get("scheduled_workout_id"),
                    _json(session),
                    record.get("created_at") or now,
                    now,
                ),
            )
            rows += 1
    return rows


def upsert_activities(data_dir: Path | str, samples: Sequence[Any]) -> int:
    if not database_enabled():
        return 0
    initialize(data_dir)
    now = datetime.now().astimezone().isoformat()
    rows = 0
    with connect(data_dir) as db:
        for sample in samples:
            activity_id = getattr(sample, "activity_id", None)
            if not activity_id:
                continue
            feedback = getattr(sample, "feedback", None)
            db.execute(
                """
                INSERT INTO activities(
                    activity_id, day, start_time_local, sport, name,
                    duration_minutes, distance_m, average_hr, max_hr,
                    average_power, normalized_power, max_power,
                    average_speed_mps, aerobic_training_effect,
                    anaerobic_training_effect, training_load, vo2max,
                    efficiency_score, workout_id, soreness, fatigue,
                    motivation, pain_or_injury, session_rpe, sample_json,
                    updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(activity_id) DO UPDATE SET
                    day=excluded.day,
                    start_time_local=excluded.start_time_local,
                    sport=excluded.sport,
                    name=excluded.name,
                    duration_minutes=excluded.duration_minutes,
                    distance_m=excluded.distance_m,
                    average_hr=excluded.average_hr,
                    max_hr=excluded.max_hr,
                    average_power=excluded.average_power,
                    normalized_power=excluded.normalized_power,
                    max_power=excluded.max_power,
                    average_speed_mps=excluded.average_speed_mps,
                    aerobic_training_effect=excluded.aerobic_training_effect,
                    anaerobic_training_effect=excluded.anaerobic_training_effect,
                    training_load=excluded.training_load,
                    vo2max=excluded.vo2max,
                    efficiency_score=excluded.efficiency_score,
                    workout_id=excluded.workout_id,
                    soreness=excluded.soreness,
                    fatigue=excluded.fatigue,
                    motivation=excluded.motivation,
                    pain_or_injury=excluded.pain_or_injury,
                    session_rpe=excluded.session_rpe,
                    sample_json=excluded.sample_json,
                    updated_at=excluded.updated_at
                """,
                (
                    int(activity_id),
                    str(getattr(sample, "day")),
                    getattr(sample, "start_time_local", None),
                    str(getattr(sample, "sport")),
                    getattr(sample, "name", None),
                    int(getattr(sample, "duration_minutes", 0)),
                    getattr(sample, "distance_m", None),
                    getattr(sample, "average_hr", None),
                    getattr(sample, "max_hr", None),
                    getattr(sample, "average_power", None),
                    getattr(sample, "normalized_power", None),
                    getattr(sample, "max_power", None),
                    getattr(sample, "average_speed_mps", None),
                    getattr(sample, "aerobic_training_effect", None),
                    getattr(sample, "anaerobic_training_effect", None),
                    getattr(sample, "training_load", None),
                    getattr(sample, "vo2max", None),
                    getattr(sample, "efficiency_score", None),
                    getattr(sample, "workout_id", None),
                    getattr(feedback, "soreness", None),
                    getattr(feedback, "fatigue", None),
                    getattr(feedback, "motivation", None),
                    _bool_int(getattr(feedback, "pain_or_injury", None)),
                    getattr(feedback, "session_rpe", None),
                    _json(sample),
                    now,
                ),
            )
            rows += 1
    return rows


def replace_matches(data_dir: Path | str, result: MatchResult) -> int:
    if not database_enabled():
        return 0
    initialize(data_dir)
    now = datetime.now().astimezone().isoformat()
    with connect(data_dir) as db:
        session_keys = list(result.unmatched_session_keys) + [
            match.session_key for match in result.matches
        ]
        activity_ids = list(result.unmatched_activity_ids) + [
            match.activity_id for match in result.matches
        ]
        if session_keys:
            placeholders = ",".join("?" for _ in session_keys)
            db.execute(
                f"DELETE FROM session_matches WHERE session_key IN ({placeholders})",
                tuple(session_keys),
            )
        if activity_ids:
            placeholders = ",".join("?" for _ in activity_ids)
            db.execute(
                f"DELETE FROM session_matches WHERE activity_id IN ({placeholders})",
                tuple(activity_ids),
            )
        for match in result.matches:
            db.execute(
                """
                INSERT INTO session_matches(
                    session_key, activity_id, score, method, day_difference,
                    duration_ratio, name_similarity, reasons_json, matched_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    match.session_key,
                    match.activity_id,
                    match.score,
                    match.method,
                    match.day_difference,
                    match.duration_ratio,
                    match.name_similarity,
                    _json(match.reasons),
                    now,
                ),
            )
    return len(result.matches)


def upsert_recovery_report(data_dir: Path | str, payload: dict[str, Any]) -> bool:
    if not database_enabled():
        return False
    metrics = payload.get("metrics") or {}
    advice = payload.get("advice") or {}
    day = metrics.get("day") or payload.get("day")
    if not day:
        return False
    initialize(data_dir)
    now = datetime.now().astimezone().isoformat()
    with connect(data_dir) as db:
        db.execute(
            """
            INSERT INTO recovery_daily(
                day, status, training_readiness, sleep_hours, sleep_score,
                hrv_ratio, resting_hr_delta, report_json, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(day) DO UPDATE SET
                status=excluded.status,
                training_readiness=excluded.training_readiness,
                sleep_hours=excluded.sleep_hours,
                sleep_score=excluded.sleep_score,
                hrv_ratio=excluded.hrv_ratio,
                resting_hr_delta=excluded.resting_hr_delta,
                report_json=excluded.report_json,
                updated_at=excluded.updated_at
            """,
            (
                str(day),
                advice.get("status"),
                metrics.get("training_readiness"),
                metrics.get("sleep_hours"),
                metrics.get("sleep_score"),
                metrics.get("hrv_ratio"),
                metrics.get("resting_hr_delta"),
                _json(payload),
                now,
            ),
        )
    return True


def import_recovery_reports(data_dir: Path | str) -> int:
    report_dir = Path(data_dir) / "reports"
    if not report_dir.exists() or not database_enabled():
        return 0
    count = 0
    for path in report_dir.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and upsert_recovery_report(data_dir, payload):
            count += 1
    return count


def save_analysis_snapshot(data_dir: Path | str, analysis: Any) -> None:
    if not database_enabled():
        return
    initialize(data_dir)
    payload = asdict(analysis) if is_dataclass(analysis) else analysis
    generated_at = str(payload.get("generated_at"))
    with connect(data_dir) as db:
        db.execute(
            """
            INSERT OR REPLACE INTO analysis_snapshots(
                generated_at, period_start, period_end, analysis_json
            ) VALUES(?,?,?,?)
            """,
            (
                generated_at,
                str(payload.get("period_start")),
                str(payload.get("period_end")),
                _json(payload),
            ),
        )


def upsert_benchmark_result(data_dir: Path | str, result: dict[str, Any]) -> bool:
    if not database_enabled() or not result.get("activity_id") or not result.get("benchmark_id"):
        return False
    initialize(data_dir)
    with connect(data_dir) as db:
        db.execute(
            """
            INSERT INTO benchmark_results(
                benchmark_id, sport, activity_id, session_key, day,
                result_value, result_unit, secondary_value, secondary_unit,
                confidence, method, details_json, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(activity_id) DO UPDATE SET
                benchmark_id=excluded.benchmark_id,
                sport=excluded.sport,
                session_key=excluded.session_key,
                day=excluded.day,
                result_value=excluded.result_value,
                result_unit=excluded.result_unit,
                secondary_value=excluded.secondary_value,
                secondary_unit=excluded.secondary_unit,
                confidence=excluded.confidence,
                method=excluded.method,
                details_json=excluded.details_json,
                created_at=excluded.created_at
            """,
            (
                result["benchmark_id"],
                result["sport"],
                int(result["activity_id"]),
                result.get("session_key"),
                result["day"],
                result.get("result_value"),
                result.get("result_unit"),
                result.get("secondary_value"),
                result.get("secondary_unit"),
                float(result.get("confidence", 0.0)),
                result.get("method", "summary"),
                _json(result),
                datetime.now().astimezone().isoformat(),
            ),
        )
    return True


def summary(data_dir: Path | str) -> dict[str, Any]:
    path = database_path(data_dir)
    if not path.exists():
        return {
            "enabled": database_enabled(),
            "path": str(path),
            "initialized": False,
            "planned_sessions": 0,
            "activities": 0,
            "matches": 0,
            "benchmark_results": 0,
            "match_rate": None,
        }
    with connect(data_dir, readonly=True) as db:
        counts = {
            table: int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "planned_sessions",
                "activities",
                "session_matches",
                "benchmark_results",
                "recovery_daily",
            )
        }
        history_days = max(28, int(os.getenv("MEASUREMENT_HISTORY_DAYS", "180")))
        cutoff = date.fromordinal(date.today().toordinal() - history_days + 1).isoformat()
        eligible = int(
            db.execute(
                """
                SELECT COUNT(*) FROM planned_sessions
                WHERE day BETWEEN ? AND ? AND status != 'cancelled'
                """,
                (cutoff, date.today().isoformat()),
            ).fetchone()[0]
        )
        matched = int(
            db.execute(
                """
                SELECT COUNT(*) FROM session_matches m
                JOIN planned_sessions p ON p.session_key=m.session_key
                WHERE p.day BETWEEN ? AND ? AND p.status != 'cancelled'
                """,
                (cutoff, date.today().isoformat()),
            ).fetchone()[0]
        )
        latest_benchmarks = [
            dict(row)
            for row in db.execute(
                """
                SELECT benchmark_id, sport, day, result_value, result_unit,
                       secondary_value, secondary_unit, confidence, method
                FROM benchmark_results
                ORDER BY day DESC LIMIT 6
                """
            ).fetchall()
        ]
        review_matches = [
            dict(row)
            for row in db.execute(
                """
                SELECT p.day, p.sport, p.name AS planned_name,
                       a.name AS activity_name, m.score, m.method,
                       m.duration_ratio, m.name_similarity
                FROM session_matches m
                JOIN planned_sessions p ON p.session_key=m.session_key
                JOIN activities a ON a.activity_id=m.activity_id
                WHERE p.day BETWEEN ? AND ? AND m.score < 85
                ORDER BY p.day DESC, m.score ASC
                LIMIT 8
                """,
                (cutoff, date.today().isoformat()),
            ).fetchall()
        ]
        unmatched_planned = [
            dict(row)
            for row in db.execute(
                """
                SELECT p.session_key, p.day, p.sport, p.name, p.duration_minutes
                FROM planned_sessions p
                LEFT JOIN session_matches m ON m.session_key=p.session_key
                WHERE p.day BETWEEN ? AND ?
                  AND p.status != 'cancelled'
                  AND m.session_key IS NULL
                ORDER BY p.day DESC
                LIMIT 8
                """,
                (cutoff, date.today().isoformat()),
            ).fetchall()
        ]
    return {
        "enabled": database_enabled(),
        "path": str(path),
        "initialized": True,
        **counts,
        "match_rate": round(matched / eligible, 3) if eligible else None,
        "latest_benchmarks": latest_benchmarks,
        "review_matches": review_matches,
        "unmatched_planned": unmatched_planned,
    }
