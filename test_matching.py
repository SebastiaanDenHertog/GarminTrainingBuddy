from __future__ import annotations

import unittest
from dataclasses import dataclass

from matching import match_planned_sessions


@dataclass
class Activity:
    activity_id: int
    day: str
    sport: str
    duration_minutes: int
    name: str
    workout_id: int | None = None


class MatchingTests(unittest.TestCase):
    def test_workout_id_wins_for_double_session_day(self) -> None:
        planned = [
            {
                "key": "2026-07-24:run:easy:0",
                "day": "2026-07-24",
                "sport": "run",
                "role": "easy",
                "duration_minutes": 40,
                "name": "AI IRONMAN V5 Easy Run 40 min",
                "_record_workout_id": 100,
            },
            {
                "key": "2026-07-24:run:brick:1",
                "day": "2026-07-24",
                "sport": "run",
                "role": "brick",
                "duration_minutes": 20,
                "name": "AI IRONMAN V5 Brick Run 20 min",
                "_record_workout_id": 200,
            },
        ]
        activities = [
            Activity(1, "2026-07-24", "run", 21, "Afternoon Run", 200),
            Activity(2, "2026-07-24", "run", 42, "Easy Run", 100),
        ]
        result = match_planned_sessions(planned, activities)
        by_session = {item.session_key: item.activity_id for item in result.matches}
        self.assertEqual(by_session[planned[0]["key"]], 2)
        self.assertEqual(by_session[planned[1]["key"]], 1)
        self.assertFalse(result.unmatched_session_keys)

    def test_low_similarity_wrong_day_is_not_matched(self) -> None:
        planned = [
            {
                "key": "x",
                "day": "2026-07-24",
                "sport": "bike",
                "role": "quality",
                "duration_minutes": 60,
                "name": "Bike intervals",
            }
        ]
        activities = [Activity(3, "2026-07-27", "bike", 15, "Commute")]
        result = match_planned_sessions(planned, activities, day_tolerance=1)
        self.assertEqual(result.matches, ())
        self.assertEqual(result.unmatched_session_keys, ("x",))


if __name__ == "__main__":
    unittest.main()
