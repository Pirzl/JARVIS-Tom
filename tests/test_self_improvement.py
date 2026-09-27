"""
Unit test for JARVIS Self-Improvement Engine & Execution Logger Telemetry.
"""
from __future__ import annotations

import unittest
from pathlib import Path
import sys

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core.execution_logger import log_execution, get_action_stats, get_recent_failures
from actions.self_improvement import run_self_audit
from core.action_loader import discover_actions


class TestSelfImprovement(unittest.TestCase):
    def test_execution_logger(self):
        log_execution("unit_test_action", {"arg": 1}, success=True, execution_time_ms=25.0)
        log_execution("unit_test_failure", {"query": "fail"}, success=False, error_message="Test failure", execution_time_ms=10.0)

        stats = get_action_stats(hours=24)
        failures = get_recent_failures(hours=24)

        self.assertGreaterEqual(stats["total_runs"], 1)
        self.assertGreaterEqual(len(failures), 1)

    def test_action_discovery(self):
        actions = discover_actions(BASE_DIR / "actions", set(), logger=lambda _: None)
        self.assertIn("run_self_audit", actions.names())

    def test_self_audit_handler(self):
        res = run_self_audit({"auto_fix": False})
        self.assertIn("Completed daily self-audit", res)


if __name__ == "__main__":
    unittest.main()
