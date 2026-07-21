from __future__ import annotations

import json
import unittest
from unittest.mock import patch
from dataclasses import replace

from analytics import TrainingAnalysis
from coach import (
    deterministic_proposal,
    load_evidence,
    request_embedded_ai_proposal,
    validate_proposal,
)


def analysis(**overrides):
    base = TrainingAnalysis(
        generated_at="2026-07-21T12:00:00+12:00",
        period_start="2025-09-29",
        period_end="2026-07-21",
        activity_count=20,
        total_minutes=1600,
        by_sport_minutes={"run": 500, "bike": 800, "swim": 300},
        planned_sessions=20,
        matched_sessions=18,
        compliance=0.9,
        completion_by_sport={"run": 0.9, "bike": 0.9, "swim": 0.9},
        efficiency_change_percent={"run": 2.0, "bike": 1.6, "swim": 0.5},
        recent_14d_minutes=600,
        previous_14d_minutes=580,
        volume_change_percent=3.4,
        hard_sessions_recent_7d=1,
        pain_reported=False,
        high_soreness_reported=False,
        high_fatigue_reported=False,
        low_motivation_reported=False,
        feedback_entries=5,
        average_rpe=5.5,
        recovery_red_days=0,
        recovery_amber_days=2,
        average_training_readiness=68,
        average_sleep_score=78,
        average_hrv_ratio=1.01,
        current_week_index=5,
        recovery_week=False,
        weekly_summaries=[],
        samples=[],
    )
    return replace(base, **overrides)


CONFIG = {"plan": {"max_hard_sessions_per_week": 2}}


class CoachTests(unittest.TestCase):
    def setUp(self) -> None:
        self.evidence = load_evidence("config/evidence.yaml")

    def test_progress_when_improving_and_recovered(self) -> None:
        proposal = deterministic_proposal(analysis(), CONFIG)
        self.assertEqual(proposal.decision, "progress")
        result = validate_proposal(proposal, analysis(), CONFIG, self.evidence)
        self.assertTrue(result.accepted, result.errors)

    def test_pain_forces_reduction(self) -> None:
        current = analysis(pain_reported=True)
        proposal = deterministic_proposal(current, CONFIG)
        self.assertEqual(proposal.decision, "reduce")
        result = validate_proposal(proposal, current, CONFIG, self.evidence)
        self.assertTrue(result.accepted, result.errors)

    def test_progress_blocked_in_recovery_week(self) -> None:
        current = analysis(recovery_week=True)
        proposal = deterministic_proposal(analysis(), CONFIG)
        result = validate_proposal(proposal, current, CONFIG, self.evidence)
        self.assertFalse(result.accepted)

    @patch("local_ai.generate_chat_completion")
    def test_embedded_ai_returns_structured_proposal(self, generate) -> None:
        generate.return_value = json.dumps(
            {
                "decision": "hold",
                "confidence": 0.9,
                "weekly_volume_change_percent": 0,
                "sport_volume_change_percent": {"run": 0, "bike": 0, "swim": 0},
                "quality_changes": [],
                "reasoning_summary": ["Current training is working."],
                "evidence_ids": ["HOLD_WHEN_UNCERTAIN_001"],
                "valid_for_weeks": 1,
            }
        )
        current = analysis()
        deterministic = deterministic_proposal(current, CONFIG)
        proposal = request_embedded_ai_proposal(
            current, deterministic, self.evidence
        )
        self.assertEqual(proposal.source, "local_ai")
        self.assertEqual(proposal.decision, "hold")
        self.assertTrue(
            validate_proposal(proposal, current, CONFIG, self.evidence).accepted
        )



if __name__ == "__main__":
    unittest.main()
