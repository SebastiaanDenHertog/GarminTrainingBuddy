from __future__ import annotations

import unittest
from datetime import date, timedelta
from pathlib import Path

from planner import (
    DEFAULT_CONFIG,
    build_sessions_for_range,
    build_week_sessions,
    workout_for_session,
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


if __name__ == "__main__":
    unittest.main()
