from __future__ import annotations

import os
import unittest
from dataclasses import dataclass
from unittest.mock import patch

from benchmarks import apply_due_benchmark, due_protocols


@dataclass(frozen=True)
class FakeSession:
    day: str
    sport: str
    role: str
    duration_minutes: int
    name: str
    description: str
    intensity: str
    primary: bool = True
    benchmark_id: str | None = None


class BenchmarkTests(unittest.TestCase):
    def config(self) -> dict:
        return {
            "plan": {"name_prefix": "TEST"},
            "measurement": {
                "benchmarks": {
                    "enabled": True,
                    "auto_schedule": True,
                    "interval_weeks": 8,
                    "first_test_week": 2,
                    "sports": {
                        "run": {
                            "protocol": "run_aerobic_30",
                            "offset_weeks": 0,
                            "preferred_weekday": "thursday",
                            "replace_roles": ["quality", "easy"],
                        }
                    },
                }
            },
        }

    def test_due_protocol_and_replacement(self) -> None:
        with patch.dict(
            os.environ,
            {"BENCHMARKS_ENABLED": "true", "BENCHMARK_AUTO_SCHEDULE": "true"},
            clear=False,
        ):
            self.assertEqual(due_protocols(self.config(), 2), [("run", "run_aerobic_30")])
            sessions = [
                FakeSession(
                    "2026-07-30", "run", "quality", 45, "Quality Run", "old", "hard"
                )
            ]
            result = apply_due_benchmark(
                self.config(), sessions, week_index=2, recovery_week=False
            )
        self.assertEqual(result[0].role, "benchmark")
        self.assertEqual(result[0].benchmark_id, "run_aerobic_30")
        self.assertGreaterEqual(result[0].duration_minutes, 50)


    def test_hard_benchmark_downgrades_nearby_quality_session(self) -> None:
        config = {
            "plan": {
                "name_prefix": "TEST",
                "max_hard_sessions_per_week": 2,
                "max_hard_sessions_per_day": 1,
            },
            "measurement": {
                "benchmarks": {
                    "enabled": True,
                    "auto_schedule": True,
                    "interval_weeks": 8,
                    "first_test_week": 2,
                    "sports": {
                        "swim": {
                            "protocol": "swim_css_400_200",
                            "offset_weeks": 0,
                            "preferred_weekday": "friday",
                            "replace_roles": ["endurance"],
                        }
                    },
                }
            },
        }
        sessions = [
            FakeSession("2026-07-28", "bike", "quality", 60, "Bike quality", "", "hard"),
            FakeSession("2026-07-30", "run", "quality", 55, "Run quality", "", "hard"),
            FakeSession("2026-07-31", "swim", "endurance", 60, "Swim", "", "moderate"),
        ]
        with patch.dict(
            os.environ,
            {"BENCHMARKS_ENABLED": "true", "BENCHMARK_AUTO_SCHEDULE": "true"},
            clear=False,
        ):
            result = apply_due_benchmark(
                config, sessions, week_index=2, recovery_week=False
            )
        self.assertEqual(sum(item.intensity == "hard" for item in result), 2)
        self.assertTrue(any(item.benchmark_id == "swim_css_400_200" for item in result))
        self.assertTrue(any("Benchmark support" in item.name for item in result))

    def test_due_on_recovery_week_moves_to_following_week(self) -> None:
        config = self.config()
        config["measurement"]["benchmarks"]["sports"]["run"]["offset_weeks"] = 1
        with patch.dict(
            os.environ,
            {"BENCHMARKS_ENABLED": "true", "BENCHMARK_AUTO_SCHEDULE": "true"},
            clear=False,
        ):
            self.assertEqual(due_protocols(config, 3), [])
            self.assertEqual(due_protocols(config, 4), [("run", "run_aerobic_30")])

    def test_recovery_week_never_gets_benchmark(self) -> None:
        with patch.dict(
            os.environ,
            {"BENCHMARKS_ENABLED": "true", "BENCHMARK_AUTO_SCHEDULE": "true"},
            clear=False,
        ):
            original = [FakeSession("2026-07-30", "run", "quality", 45, "Run", "", "hard")]
            result = apply_due_benchmark(
                self.config(), original, week_index=2, recovery_week=True
            )
        self.assertEqual(result, original)


if __name__ == "__main__":
    unittest.main()
