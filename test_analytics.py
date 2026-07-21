from __future__ import annotations

import os
import unittest
from datetime import date
from unittest.mock import patch

from analytics import _structured_training_context


class TrainingAgeTests(unittest.TestCase):
    def test_valid_structured_training_start_date(self) -> None:
        with patch.dict(
            os.environ,
            {"STRUCTURED_TRAINING_START_DATE": "2026-01-01"},
            clear=False,
        ):
            start, weeks, band, warning = _structured_training_context(
                date(2026, 7, 22)
            )
        self.assertEqual(start, "2026-01-01")
        self.assertEqual(weeks, 28)
        self.assertEqual(band, "established")
        self.assertIsNone(warning)

    def test_invalid_date_is_ignored_with_warning(self) -> None:
        with patch.dict(
            os.environ,
            {"STRUCTURED_TRAINING_START_DATE": "01-01-2026"},
            clear=False,
        ):
            start, weeks, band, warning = _structured_training_context(
                date(2026, 7, 22)
            )
        self.assertIsNone(start)
        self.assertIsNone(weeks)
        self.assertEqual(band, "unknown")
        self.assertIn("YYYY-MM-DD", warning or "")


if __name__ == "__main__":
    unittest.main()
