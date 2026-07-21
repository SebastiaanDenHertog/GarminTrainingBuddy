from __future__ import annotations

import unittest

from feedback import cache_safe_activity_payload, extract_description, parse_feedback_text


class FeedbackTests(unittest.TestCase):
    def test_compact_english_format(self) -> None:
        result = parse_feedback_text(
            "Great session. #coach soreness=3 fatigue=4 motivation=8 pain=no rpe=6"
        )
        self.assertEqual(result.soreness, 3)
        self.assertEqual(result.fatigue, 4)
        self.assertEqual(result.motivation, 8)
        self.assertFalse(result.pain_or_injury)
        self.assertEqual(result.session_rpe, 6)

    def test_multiline_and_dutch_aliases(self) -> None:
        result = parse_feedback_text(
            "Spierpijn: 7\nVermoeidheid: 6\nMotivatie: 4\nBlessure: nee\nInspanning: 8"
        )
        self.assertEqual(result.soreness, 7)
        self.assertEqual(result.fatigue, 6)
        self.assertEqual(result.motivation, 4)
        self.assertFalse(result.pain_or_injury)
        self.assertEqual(result.session_rpe, 8)

    def test_description_extraction(self) -> None:
        payload = {
            "activityName": "Run",
            "metadata": {"activityDescription": "#coach pain=yes fatigue=8"},
        }
        self.assertEqual(
            extract_description(payload), "#coach pain=yes fatigue=8"
        )

    def test_placeholder_is_not_parsed_as_real_feedback(self) -> None:
        result = parse_feedback_text(
            "#coach soreness=[0-10] fatigue=[0-10] motivation=[0-10] "
            "pain=[yes/no] rpe=[1-10]"
        )
        self.assertFalse(result.has_values)

    def test_cache_redacts_private_description(self) -> None:
        payload = {
            "activityId": 42,
            "activityDescription": "Private note #coach fatigue=5 pain=no",
            "averageHR": 140,
        }
        cached = cache_safe_activity_payload(payload)
        self.assertIsNone(cached["activityDescription"])
        self.assertEqual(cached["_coach_feedback_cache"]["fatigue"], 5)
        self.assertFalse(cached["_coach_feedback_cache"]["pain_or_injury"])


if __name__ == "__main__":
    unittest.main()
