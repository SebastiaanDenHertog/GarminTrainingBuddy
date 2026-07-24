from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from feedback import ActivityFeedback
from matching import MatchResult, SessionMatch
from training_db import (
    initialize,
    replace_matches,
    summary,
    sync_planner_state,
    upsert_activities,
)


@dataclass
class Sample:
    activity_id: int
    day: str
    start_time_local: str | None
    sport: str
    name: str
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


class TrainingDatabaseTests(unittest.TestCase):
    def test_database_stores_plan_activity_and_match(self) -> None:
        with tempfile.TemporaryDirectory() as raw, patch.dict(
            os.environ,
            {"TRAINING_DB_ENABLED": "true", "TRAINING_DB_PATH": str(Path(raw) / "db.sqlite")},
            clear=False,
        ):
            initialize(raw)
            state = {
                "sessions": {
                    "session-1": {
                        "status": "scheduled",
                        "workout_id": 100,
                        "scheduled_workout_id": 200,
                        "fingerprint": "abc",
                        "session": {
                            "key": "session-1",
                            "day": "2026-07-24",
                            "sport": "run",
                            "role": "easy",
                            "name": "Easy Run",
                            "duration_minutes": 40,
                            "primary": True,
                        },
                    }
                }
            }
            self.assertEqual(sync_planner_state(raw, state), 1)
            feedback = ActivityFeedback(1, "2026-07-24", "Easy Run", session_rpe=5)
            sample = Sample(
                1, "2026-07-24", "2026-07-24T10:00:00", "run", "Easy Run", 41,
                8000, 140, 160, None, None, None, 3.2, 2.5, 0.0, 70, 52,
                round(3.2 / 140, 7), 100, feedback,
            )
            self.assertEqual(upsert_activities(raw, [sample]), 1)
            result = MatchResult(
                matches=(
                    SessionMatch(
                        "session-1", 1, 98.0, "garmin_workout_id", 0, 0.98, 0.9,
                        ("same calendar day", "Garmin workout ID matches"),
                    ),
                ),
                unmatched_session_keys=(),
                unmatched_activity_ids=(),
            )
            self.assertEqual(replace_matches(raw, result), 1)
            info = summary(raw)
            self.assertTrue(info["initialized"])
            self.assertEqual(info["planned_sessions"], 1)
            self.assertEqual(info["activities"], 1)
            self.assertEqual(info["session_matches"], 1)


if __name__ == "__main__":
    unittest.main()
