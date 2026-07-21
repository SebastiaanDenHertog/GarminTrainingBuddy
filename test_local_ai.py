from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from local_ai import check_available_ram, estimate_model_ram_gb


class LocalAiRamTests(unittest.TestCase):
    def test_qwen_30b_estimate_is_conservative(self) -> None:
        estimate = estimate_model_ram_gb("Qwen/Qwen3-30B-A3B", "auto")
        self.assertGreater(estimate, 70)
        self.assertLess(estimate, 90)

    @patch("local_ai._cgroup_memory_bytes", return_value=(None, None, None))
    @patch("local_ai._host_memory_bytes")
    def test_strict_mode_blocks_insufficient_ram(
        self, host_memory, _cgroup_memory
    ) -> None:
        gib = 1024**3
        host_memory.return_value = (64 * gib, 50 * gib)
        with patch.dict(
            os.environ,
            {
                "AI_MODEL": "Qwen/Qwen3-30B-A3B",
                "LOCAL_AI_RAM_CHECK_MODE": "strict",
                "LOCAL_AI_REQUIRED_RAM_GB": "70",
                "LOCAL_AI_RAM_RESERVE_GB": "8",
            },
            clear=False,
        ):
            result = check_available_ram()
        self.assertFalse(result.enough_ram)
        self.assertFalse(result.load_allowed)

    @patch("local_ai._cgroup_memory_bytes")
    @patch("local_ai._host_memory_bytes")
    def test_cgroup_remaining_is_used_as_effective_limit(
        self, host_memory, cgroup_memory
    ) -> None:
        gib = 1024**3
        host_memory.return_value = (256 * gib, 220 * gib)
        cgroup_memory.return_value = (96 * gib, 84 * gib, "v2")
        with patch.dict(
            os.environ,
            {
                "AI_MODEL": "Qwen/Qwen3-30B-A3B",
                "LOCAL_AI_RAM_CHECK_MODE": "strict",
                "LOCAL_AI_REQUIRED_RAM_GB": "70",
                "LOCAL_AI_RAM_RESERVE_GB": "8",
            },
            clear=False,
        ):
            result = check_available_ram()
        self.assertEqual(result.effective_available_gb, 84.0)
        self.assertTrue(result.enough_ram)
        self.assertTrue(result.load_allowed)

    @patch("local_ai._cgroup_memory_bytes", return_value=(None, None, None))
    @patch("local_ai._host_memory_bytes")
    def test_warn_mode_reports_shortage_but_allows_loading(
        self, host_memory, _cgroup_memory
    ) -> None:
        gib = 1024**3
        host_memory.return_value = (64 * gib, 50 * gib)
        with patch.dict(
            os.environ,
            {
                "LOCAL_AI_RAM_CHECK_MODE": "warn",
                "LOCAL_AI_REQUIRED_RAM_GB": "70",
                "LOCAL_AI_RAM_RESERVE_GB": "8",
            },
            clear=False,
        ):
            result = check_available_ram()
        self.assertFalse(result.enough_ram)
        self.assertTrue(result.load_allowed)


if __name__ == "__main__":
    unittest.main()
