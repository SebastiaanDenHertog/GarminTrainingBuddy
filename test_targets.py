from __future__ import annotations

import copy
import os
import sqlite3
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from targets import (
    annotate_sessions,
    parse_pace_seconds,
    resolve_training_targets,
    target_for_session,
)


@dataclass(frozen=True)
class FakeSession:
    sport: str
    role: str
    phase: str = "foundation"
    benchmark_id: str | None = None
    description: str = "Base description."
    target_kind: str | None = None
    target_low: float | None = None
    target_high: float | None = None
    target_unit: str | None = None
    target_summary: str | None = None
    target_source: str | None = None


BASE_CONFIG = {
    "workout_targets": {
        "enabled": True,
        "benchmark_conversions": {
            "minimum_confidence": 0.65,
            "run_20min_to_threshold_pace_factor": 1.03,
            "bike_20min_to_ftp_factor": 0.95,
        },
        "manual": {},
        "ranges": {
            "run_easy": {
                "hr_pct_lthr": [0.75, 0.85],
                "pace_multiplier": [1.20, 1.40],
            },
            "run_quality": {
                "foundation": {
                    "pace_multiplier": [0.94, 1.02],
                    "hr_pct_lthr": [0.90, 0.98],
                }
            },
            "bike_quality": {
                "foundation": {
                    "ftp_pct": [0.88, 1.00],
                    "hr_pct_lthr": [0.88, 0.97],
                }
            },
        },
    }
}


class TargetTests(unittest.TestCase):
    def test_parse_pace(self) -> None:
        self.assertEqual(parse_pace_seconds("4:30"), 270.0)
        self.assertEqual(parse_pace_seconds(300), 300.0)
        self.assertIsNone(parse_pace_seconds("4:99"))

    def test_manual_values_override_database(self) -> None:
        config = copy.deepcopy(BASE_CONFIG)
        config["workout_targets"]["manual"] = {
            "run_threshold_pace": "4:30",
            "run_threshold_hr_bpm": 175,
        }
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            profile = resolve_training_targets(config, Path(tmp))
        self.assertEqual(profile["run_threshold_pace_sec_per_km"], 270.0)
        self.assertEqual(profile["run_threshold_hr_bpm"], 175.0)
        self.assertEqual(profile["sources"]["run_threshold_pace_sec_per_km"], "manual/env")

    def test_confident_run_benchmark_is_converted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "training.db"
            with sqlite3.connect(db_path) as db:
                db.execute(
                    """
                    CREATE TABLE benchmark_results(
                        benchmark_id TEXT, sport TEXT, day TEXT,
                        result_value REAL, result_unit TEXT,
                        secondary_value REAL, secondary_unit TEXT,
                        confidence REAL, method TEXT
                    )
                    """
                )
                db.execute(
                    "INSERT INTO benchmark_results VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        "run_threshold_20", "run", "2026-07-20",
                        300.0, "sec/km", 175.0, "bpm", 0.90, "lap",
                    ),
                )
            with patch.dict(os.environ, {}, clear=True):
                profile = resolve_training_targets(BASE_CONFIG, Path(tmp))
        self.assertEqual(profile["run_threshold_pace_sec_per_km"], 309.0)
        self.assertEqual(profile["run_threshold_hr_bpm"], 175.0)

    def test_low_confidence_benchmark_is_not_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "training.db"
            with sqlite3.connect(db_path) as db:
                db.execute(
                    """
                    CREATE TABLE benchmark_results(
                        benchmark_id TEXT, sport TEXT, day TEXT,
                        result_value REAL, result_unit TEXT,
                        secondary_value REAL, secondary_unit TEXT,
                        confidence REAL, method TEXT
                    )
                    """
                )
                db.execute(
                    "INSERT INTO benchmark_results VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        "run_threshold_20", "run", "2026-07-20",
                        300.0, "sec/km", 175.0, "bpm", 0.45, "summary",
                    ),
                )
            with patch.dict(os.environ, {}, clear=True):
                profile = resolve_training_targets(BASE_CONFIG, Path(tmp))
        self.assertIsNone(profile["run_threshold_pace_sec_per_km"])
        self.assertIsNone(profile["run_threshold_hr_bpm"])

    def test_run_quality_prefers_pace(self) -> None:
        config = copy.deepcopy(BASE_CONFIG)
        config["_resolved_targets"] = {
            "enabled": True,
            "run_threshold_pace_sec_per_km": 300.0,
            "run_threshold_hr_bpm": 175.0,
            "sources": {
                "run_threshold_pace_sec_per_km": "test pace",
                "run_threshold_hr_bpm": "test HR",
            },
        }
        target = target_for_session(config, FakeSession("run", "quality"))
        self.assertEqual(target["kind"], "pace")
        self.assertIn("/km", target["summary"])

    def test_run_quality_uses_hr_when_pace_is_missing(self) -> None:
        config = copy.deepcopy(BASE_CONFIG)
        config["_resolved_targets"] = {
            "enabled": True,
            "run_threshold_hr_bpm": 175.0,
            "sources": {"run_threshold_hr_bpm": "test HR"},
        }
        target = target_for_session(config, FakeSession("run", "quality"))
        self.assertEqual(target["kind"], "heart_rate")
        self.assertGreaterEqual(target["low"], 150)

    def test_easy_run_can_fall_back_to_pace(self) -> None:
        config = copy.deepcopy(BASE_CONFIG)
        config["_resolved_targets"] = {
            "enabled": True,
            "run_threshold_pace_sec_per_km": 300.0,
            "sources": {"run_threshold_pace_sec_per_km": "test pace"},
        }
        target = target_for_session(config, FakeSession("run", "easy"))
        self.assertEqual(target["kind"], "pace")
        self.assertIn("Run pace", target["summary"])

    def test_bike_quality_uses_hr_without_power(self) -> None:
        config = copy.deepcopy(BASE_CONFIG)
        config["_resolved_targets"] = {
            "enabled": True,
            "bike_threshold_hr_bpm": 170.0,
            "sources": {"bike_threshold_hr_bpm": "test HR"},
        }
        target = target_for_session(config, FakeSession("bike", "quality"))
        self.assertEqual(target["kind"], "heart_rate")
        self.assertGreater(target["high"], target["low"])

    def test_annotation_is_idempotent(self) -> None:
        config = copy.deepcopy(BASE_CONFIG)
        config["_resolved_targets"] = {
            "enabled": True,
            "run_threshold_hr_bpm": 175.0,
            "sources": {"run_threshold_hr_bpm": "test HR"},
        }
        once = annotate_sessions(config, [FakeSession("run", "easy")])[0]
        twice = annotate_sessions(config, [once])[0]
        self.assertEqual(twice.description.count("Target guidance:"), 1)


if __name__ == "__main__":
    unittest.main()
