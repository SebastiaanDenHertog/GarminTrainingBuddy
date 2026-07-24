from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from dashboard import build_dashboard_payload


class DashboardTests(unittest.TestCase):
    def test_dashboard_reads_report_and_strength_plan(self) -> None:
        today = date.today()
        strength_day = today + timedelta(days=1)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "reports").mkdir()
            (root / "analytics").mkdir()
            (root / "reports" / f"{today.isoformat()}.json").write_text(
                json.dumps(
                    {
                        "metrics": {
                            "day": today.isoformat(),
                            "sleep_score": 80,
                            "training_readiness": 72,
                            "hrv_ratio": 1.02,
                            "resting_hr_delta": -1,
                            "subjective_feedback": {
                                "max_soreness": 3,
                                "max_fatigue": 4,
                                "min_motivation": 8,
                                "max_session_rpe": 6,
                                "pain_or_injury_reported": False,
                            },
                        },
                        "advice": {
                            "status": "GREEN",
                            "recommendation": "Keep the plan.",
                            "reasons": ["Recovery is stable."],
                        },
                        "plan": {
                            "planned_sessions": [
                                {
                                    "day": strength_day.isoformat(),
                                    "sport": "strength",
                                    "role": "strength",
                                    "name": "Strength",
                                    "duration_minutes": 40,
                                    "intensity": "moderate",
                                    "strength_template": "full_body_a",
                                    "strength_exercises": [
                                        {"name": "Goblet squat", "reps": "6-8"}
                                    ],
                                }
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"DATA_DIR": raw, "AI_ENABLED": "false"},
                clear=False,
            ):
                payload = build_dashboard_payload()
            self.assertEqual(payload["recovery"]["status"], "GREEN")
            self.assertEqual(payload["next_7_days"]["strength"], 40)
            self.assertEqual(payload["upcoming_sessions"][0]["sport"], "strength")
            self.assertEqual(
                payload["upcoming_sessions"][0]["strength_exercises"][0]["name"],
                "Goblet squat",
            )
            self.assertIn("ai", payload)


if __name__ == "__main__":
    unittest.main()
