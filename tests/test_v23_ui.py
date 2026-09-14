"""Behavioral checks for the shipped dependency-free browser UI.

Node is a test runner only; serving the application still needs only Python.
All DOM, network, and timers are isolated in memory.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class UIBehaviorV23Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("Node is required only for isolated UI behavior validation")

    def run_scenario(self, scenario: str) -> None:
        result = subprocess.run(
            [self.node, str(PROJECT_ROOT / "tests" / "v23_ui_behavior.cjs"), str(PROJECT_ROOT), scenario],
            cwd=PROJECT_ROOT, text=True, capture_output=True, timeout=20, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"scenario": scenario, "status": "PASS"})

    def test_units_and_coverage_are_visible_without_search_recalculation(self):
        self.run_scenario("units_and_coverage")

    def test_nulls_are_last_in_both_numeric_sort_directions(self):
        self.run_scenario("null_sorting")

    def test_history_preserves_dates_and_breaks_at_missing_or_nonpositive_values(self):
        self.run_scenario("history_gaps")

    def test_poll_recovers_without_switching_snapshot_or_regressing_progress(self):
        self.run_scenario("poll_recovers")

    def test_poll_retry_budget_retains_job_and_allows_manual_read_retry(self):
        self.run_scenario("poll_retry_limit")

    def test_refresh_resumes_active_job_during_publication(self):
        self.run_scenario("refresh_restores_job")

    def test_lost_post_response_discovers_job_without_second_submission(self):
        self.run_scenario("active_discovery_and_lost_post")

    def test_archive_read_failure_does_not_poison_cache(self):
        self.run_scenario("archive_retry")

    def test_sort_buttons_and_scatter_keyboard_activation(self):
        self.run_scenario("keyboard_controls")

    def test_return_to_current_snapshot_uses_only_get(self):
        self.run_scenario("current_snapshot_readonly")

    def test_only_latest_catalog_request_can_replace_browsable_snapshot(self):
        self.run_scenario("catalog_responses_ordered")


if __name__ == "__main__":
    unittest.main()
