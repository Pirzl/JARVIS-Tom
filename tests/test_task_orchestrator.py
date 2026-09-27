import threading
import unittest

from core.task_orchestrator import TaskOrchestrator


class _Player:
    def __init__(self):
        self.logs = []
        self.states = []

    def write_log(self, message):
        self.logs.append(message)

    def set_state(self, state):
        self.states.append(state)


class TaskOrchestratorTests(unittest.TestCase):
    def test_runs_tasks_in_parallel_and_reports_progress(self):
        orchestrator = TaskOrchestrator(max_workers=2)
        started = []
        release = threading.Event()

        def worker(parameters, player=None):
            started.append(parameters["name"])
            release.wait(timeout=2)
            return f"done:{parameters['name']}"

        player = _Player()
        result_holder = {}

        def run_group():
            group_id, records = orchestrator.start(
                [{"kind": "test", "parameters": {"name": "a"}},
                 {"kind": "test", "parameters": {"name": "b"}}],
                {"test": worker},
                player=player,
            )
            result_holder["group"] = group_id
            result_holder["records"] = records

        thread = threading.Thread(target=run_group)
        thread.start()
        for _ in range(20):
            if len(started) == 2:
                break
            threading.Event().wait(0.01)
        release.set()
        thread.join(timeout=2)

        self.assertEqual(sorted(started), ["a", "b"])
        self.assertEqual([record["status"] for record in result_holder["records"]], ["completed", "completed"])
        self.assertTrue(any("started" in message for message in player.logs))
        self.assertTrue(any(state.startswith("WORKING:test") for state in player.states))

    def test_rejects_unknown_worker_and_limits_task_count(self):
        orchestrator = TaskOrchestrator()
        with self.assertRaises(ValueError):
            orchestrator.start([{"kind": "unknown", "parameters": {}}], {})
        with self.assertRaises(ValueError):
            orchestrator.start([{"kind": "test", "parameters": {}}] * 7, {"test": lambda **_: "ok"})


if __name__ == "__main__":
    unittest.main()