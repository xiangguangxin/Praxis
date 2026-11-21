import json
from pathlib import Path
import tempfile
import unittest
from praxis_agent.agent import run
from praxis_agent.observability import EventJournal
from praxis_agent.state import create, _record, persist
from praxis_agent.timeline import timeline, write_timeline


class TimelineTests(unittest.TestCase):
    def test_runtime_records_model_batches_observations_and_tools_without_payloads(self):
        class Backend:
            def observe(self):
                return {"summary": "password=private-marker", "tools": [{"name": "read", "inputSchema": {"type": "object"}}]}
            def act(self, action): return {"ok": True}
        class Planner:
            def __init__(self): self.usage = {"calls": 0}
            def decide(self, state, observation):
                self.usage["calls"] += 1
                if state["steps"]: return {"type": "complete", "evidence": {"done": "fixture"}}
                return {"type": "act", "action": {"name": "read", "arguments": {"text": "private-marker"}, "impact": "low"}}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "state.json"
            create(path, "Inspect", ["done"])
            state = run(path, Backend(), Planner(), trace_tools=True, journal=root / "events.jsonl")
            self.assertEqual(state["status"], "completed")
            report = timeline(path)
            self.assertEqual(len(report["models"]), 2)
            self.assertEqual(report["tools"][0]["tool"], "read")
            self.assertTrue(any(e["event"] == "observation" for e in report["events"]))
            self.assertNotIn("private-marker", json.dumps(report))
            before = path.read_bytes()
            write_timeline(path, root / "report.html")
            self.assertEqual(path.read_bytes(), before)

    def test_journal_recovers_events_beyond_checkpoint_retention(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "state.json"
            state = create(path, "Inspect", ["done"])
            state["event_journal"] = str(root / "events.jsonl")
            for index in range(130): _record(state, "observation", str(index))
            persist(path, state)
            self.assertEqual(len(state["history"]), 100)
            report = timeline(path)
            self.assertEqual(len(report["events"]), 130)
            self.assertFalse(report["partial_history"])
            self.assertTrue(timeline(path, journal=root / "absent.jsonl")["partial_history"])

    def test_report_escapes_html_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "state.json"
            state = create(path, "Inspect", ["done"])
            _record(state, "paused", "<script>evil()</script> token=private-marker")
            persist(path, state)
            target = root / "report.html"
            write_timeline(path, target)
            html = target.read_text(encoding="utf-8")
            self.assertNotIn("<script>evil()", html)
            self.assertNotIn("private-marker", html)
            with self.assertRaises(FileExistsError): write_timeline(path, target)
            with self.assertRaises(FileExistsError): write_timeline(path, path)

    def test_duplicate_and_torn_journal_entries_are_tolerated(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events.jsonl"
            journal = EventJournal(path)
            for _ in range(2): journal.emit("runtime_event", run_id="run", sequence=1, status="observation")
            with path.open("a", encoding="utf-8") as stream: stream.write('{"torn":')
            self.assertEqual(len(journal.read()), 1)

    def test_journal_failure_does_not_lose_checkpoint_events(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            state = create(path, "Inspect", ["done"])
            state["event_journal"] = folder
            _record(state, "paused", "fixture")
            persist(path, state)
            self.assertEqual(state["journal_error"], "event_journal_unavailable")
            self.assertEqual(state["history"][-1]["event"], "paused")
