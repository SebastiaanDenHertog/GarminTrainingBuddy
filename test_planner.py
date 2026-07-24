from __future__ import annotations

import copy
import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from planner import (
    DEFAULT_CONFIG,
    build_sessions_for_range,
    build_week_sessions,
    workout_for_session,
    AdaptivePlanner,
)


class PlannerTests(unittest.TestCase):
    def test_default_week_contains_endurance_and_strength(self) -> None:
        start = date(2026, 7, 20)
        sessions = build_week_sessions(DEFAULT_CONFIG, start, start, 360)
        self.assertEqual({item.sport for item in sessions}, {"run", "bike", "swim", "strength"})
        self.assertLessEqual(
            sum(item.intensity == "hard" for item in sessions),
            DEFAULT_CONFIG["plan"]["max_hard_sessions_per_week"],
        )

    def test_default_config_supports_double_days_safely(self) -> None:
        start = date(2026, 7, 20)
        sessions = build_week_sessions(DEFAULT_CONFIG, start, start, 360)
        counts = {}
        for session in sessions:
            counts[session.day] = counts.get(session.day, 0) + 1
        self.assertTrue(any(value == 2 for value in counts.values()))
        for day in counts:
            self.assertLessEqual(
                sum(s.day == day and s.intensity == "hard" for s in sessions),
                DEFAULT_CONFIG["plan"]["max_hard_sessions_per_day"],
            )

    def test_rolling_range(self) -> None:
        start = date(2026, 7, 21)
        sessions = build_sessions_for_range(
            DEFAULT_CONFIG, start, start + timedelta(days=20), start, 360
        )
        self.assertTrue(sessions)
        self.assertTrue(all(start <= date.fromisoformat(s.day) <= start + timedelta(days=20) for s in sessions))

    def test_strength_session_uses_template_and_fixed_duration(self) -> None:
        start = date(2026, 7, 20)
        sessions = build_week_sessions(DEFAULT_CONFIG, start, start, 360)
        strength = next(item for item in sessions if item.sport == "strength")
        self.assertEqual(strength.strength_template, "full_body_a")
        self.assertEqual(strength.duration_minutes, 40)
        self.assertTrue(strength.strength_exercises)
        self.assertIn("Goblet squat", strength.description)

    def test_workouts_serialize(self) -> None:
        start = date(2026, 7, 20)
        sessions = build_week_sessions(DEFAULT_CONFIG, start, start, 360)
        for session in sessions:
            payload = workout_for_session(DEFAULT_CONFIG, session).to_dict()
            self.assertEqual(payload["workoutName"], session.name)
            self.assertTrue(payload["workoutSegments"])

    def test_run_quality_uses_pace_target_when_threshold_is_known(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["_resolved_targets"] = {
            "enabled": True,
            "run_threshold_pace_sec_per_km": 270.0,
            "run_threshold_hr_bpm": 175.0,
            "sources": {
                "run_threshold_pace_sec_per_km": "test",
                "run_threshold_hr_bpm": "test",
            },
        }
        start = date(2026, 7, 20)
        base_session = next(
            item
            for item in build_week_sessions(config, start, start, 360)
            if item.sport == "run"
        )
        session = replace(
            base_session,
            role="quality",
            target_kind="pace",
            target_low=1000.0 / (270.0 * 1.02),
            target_high=1000.0 / (270.0 * 0.94),
            target_unit="m/s",
            target_summary="test pace",
        )
        self.assertEqual(session.target_kind, "pace")
        payload = workout_for_session(config, session).to_dict()
        repeat = payload["workoutSegments"][0]["workoutSteps"][1]
        hard_step = repeat["workoutSteps"][0]
        self.assertEqual(hard_step["targetType"]["workoutTargetTypeKey"], "pace.zone")
        self.assertGreater(hard_step["targetValueTwo"], hard_step["targetValueOne"])

    def test_easy_run_uses_heart_rate_target_when_lthr_is_known(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["_resolved_targets"] = {
            "enabled": True,
            "run_threshold_hr_bpm": 175.0,
            "sources": {"run_threshold_hr_bpm": "test"},
        }
        start = date(2026, 7, 20)
        session = next(
            item
            for item in build_week_sessions(config, start, start, 360)
            if item.sport == "run" and item.role == "easy"
        )
        payload = workout_for_session(config, session).to_dict()
        main = payload["workoutSegments"][0]["workoutSteps"][1]
        self.assertEqual(main["targetType"]["workoutTargetTypeKey"], "heart.rate.zone")
        self.assertGreater(main["targetValueTwo"], main["targetValueOne"])

    def test_non_publish_day_skips_when_future_sessions_exist(self) -> None:
        class Client:
            def get_activities_by_date(self, *_args, **_kwargs):
                return []

        config = copy.deepcopy(DEFAULT_CONFIG)
        config["plan"]["publish_weekday"] = "sunday"
        with tempfile.TemporaryDirectory() as tmp:
            planner = AdaptivePlanner(
                client=Client(),
                config=config,
                data_dir=Path(tmp),
                write_enabled=False,
                dry_run=True,
            )
            future_day = date(2026, 7, 23)
            planner.state["sessions"]["existing"] = {
                "session": {
                    "day": future_day.isoformat(),
                    "slot_index": 0,
                    "name": "existing",
                },
                "status": "scheduled",
            }
            result = planner.ensure_horizon(date(2026, 7, 22))
            self.assertEqual(result["status"], "publication_skipped")
            self.assertEqual(result["actions"], [])

    def test_second_sunday_normally_adds_only_the_new_week(self) -> None:
        class Client:
            def get_activities_by_date(self, *_args, **_kwargs):
                return []

        config = copy.deepcopy(DEFAULT_CONFIG)
        config["plan"]["publish_weekday"] = "sunday"
        config["plan"]["publish_weeks"] = 2
        first_sunday = date(2026, 7, 26)
        first_start = date(2026, 7, 27)
        first_end = date(2026, 8, 9)
        with tempfile.TemporaryDirectory() as tmp:
            planner = AdaptivePlanner(
                client=Client(),
                config=config,
                data_dir=Path(tmp),
                write_enabled=False,
                dry_run=True,
            )
            planner.state["plan_started_on"] = first_sunday.isoformat()
            first_sessions = build_sessions_for_range(
                planner.config, first_start, first_end, first_sunday, 360
            )
            for session in first_sessions:
                record = planner._session_record(session)
                record["status"] = "scheduled"
                planner.state["sessions"][session.key] = record

            result = planner.ensure_horizon(date(2026, 8, 2))
            created = [
                action for action in result["actions"]
                if action.get("action") == "create_and_schedule"
            ]
            self.assertTrue(created)
            self.assertTrue(
                all(action["date"] >= "2026-08-10" for action in created)
            )
            self.assertFalse(
                any(action.get("action") == "would_replace_changed_session" for action in result["actions"])
            )

    def test_sunday_publication_covers_two_full_upcoming_weeks(self) -> None:
        class Client:
            def get_activities_by_date(self, *_args, **_kwargs):
                return []

        config = copy.deepcopy(DEFAULT_CONFIG)
        config["plan"]["publish_weekday"] = "sunday"
        config["plan"]["publish_weeks"] = 2
        with tempfile.TemporaryDirectory() as tmp:
            planner = AdaptivePlanner(
                client=Client(),
                config=config,
                data_dir=Path(tmp),
                write_enabled=False,
                dry_run=True,
            )
            result = planner.ensure_horizon(date(2026, 7, 26))
            self.assertEqual(result["range"]["from"], "2026-07-27")
            self.assertEqual(result["range"]["to"], "2026-08-09")


if __name__ == "__main__":
    unittest.main()
