import json
from pathlib import Path
import tempfile
import unittest

from context_service import Project
from lifecycle import handle


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        (self.root / "pyproject.toml").touch()
        self.state = self.base / "state"
        self.project = Project(self.root, self.state)

    def event(self, name, **extra):
        return {"hook_event_name": name, "session_id": "fixture-session", "cwd": str(self.root), **extra}

    def invoke(self, name, **extra):
        return handle(self.event(name, **extra), self.state, self.root)

    def test_start_is_bounded_and_does_not_inject_notes(self):
        self.project.record("decision", "title", "PRIVATE_TEST_TEXT", "evidence")
        result = json.dumps(self.invoke("SessionStart"))
        self.assertNotIn("PRIVATE_TEST_TEXT", result)
        self.assertLess(len(result), 1600)

    def test_start_rejects_different_cwd(self):
        with self.assertRaisesRegex(ValueError, "cwd"):
            handle(self.event("SessionStart", cwd=str(self.base)), self.state, self.root)

    def test_routing_preserves_memory_and_requires_complete_evidence(self):
        text = self.invoke("SessionStart")["hookSpecificOutput"]["additionalContext"]
        for rule in ("local read/rg", "decisions or unfinished work", "continuation/read full sources",
                     "sources remain unchanged", "context_record", "Never repeat an operation with unknown outcome"):
            self.assertIn(rule, text)
        self.assertLess(len(text), 1400)

    def test_identifiers_cannot_inject_context(self):
        with self.assertRaises(ValueError):
            self.invoke("SessionStart", session_id="malformed\ncontext")

    def test_raw_tool_data_not_stored(self):
        self.invoke("PostToolUse", tool_name="apply_patch", turn_id="turn1", tool_use_id="tool1",
                    tool_input={"command": "PRIVATE_TEST_COMMAND"}, tool_response="PRIVATE_TEST_OUTPUT")
        with self.project.db() as db:
            metadata = db.execute("SELECT metadata FROM lifecycle_events").fetchone()[0]
        self.assertNotIn("PRIVATE_TEST", metadata)

    def test_unsaved_edit_warns_once_without_blocking(self):
        self.invoke("PostToolUse", tool_name="apply_patch", turn_id="turn1", tool_use_id="tool1")
        first = self.invoke("Stop", turn_id="turn1")
        self.assertIn("systemMessage", first)
        self.assertNotIn("decision", first)
        self.assertNotIn("continue", first)
        self.assertEqual(self.invoke("Stop", turn_id="turn1"), {})

    def test_saved_checkpoint_suppresses_warning(self):
        self.invoke("PostToolUse", tool_name="apply_patch", turn_id="turn1", tool_use_id="tool1")
        self.project.checkpoint("task", "fixture-session", 0, "goal", "next", "summary", "tests")
        self.assertEqual(self.invoke("PreCompact", turn_id="turn1"), {})

    def test_duplicate_edit_delivery_does_not_invalidate_checkpoint(self):
        self.invoke("PostToolUse", tool_name="apply_patch", turn_id="turn1", tool_use_id="tool1")
        self.project.checkpoint("task", "fixture-session", 0, "goal", "next", "summary", "tests")
        self.invoke("PostToolUse", tool_name="apply_patch", turn_id="turn1", tool_use_id="tool1")
        self.assertEqual(self.invoke("Stop", turn_id="turn1"), {})

    def test_distinct_edits_in_one_turn_are_not_deduplicated(self):
        self.invoke("PostToolUse", tool_name="apply_patch", turn_id="turn1", tool_use_id="tool1")
        self.project.checkpoint("task", "fixture-session", 0, "goal", "next", "summary", "tests")
        self.invoke("PostToolUse", tool_name="apply_patch", turn_id="turn1", tool_use_id="tool2")
        self.assertIn("systemMessage", self.invoke("PreCompact", turn_id="turn1"))

    def test_events_without_ids_do_not_hide_later_changes(self):
        self.invoke("PostToolUse", tool_name="apply_patch")
        self.project.checkpoint("task", "fixture-session", 0, "goal", "next", "summary", "tests")
        self.invoke("PostToolUse", tool_name="apply_patch")
        self.assertIn("systemMessage", self.invoke("Stop"))

    def test_readonly_stop_is_quiet(self):
        self.invoke("SessionStart")
        self.assertEqual(self.invoke("Stop", turn_id="turn1"), {})

    def test_session_end_never_starts_new_work(self):
        self.assertEqual(self.invoke("SessionEnd"), {})

    def test_markerless_folder_is_quiet_then_activates_after_normal_setup(self):
        blank = self.base / "new-project"
        blank.mkdir()
        event = self.event("SessionStart", cwd=str(blank))
        self.assertEqual(handle(event, self.state, blank), {})
        self.assertEqual(list(blank.iterdir()), [])
        (blank / "pyproject.toml").touch()
        self.assertIn("hookSpecificOutput", handle(event, self.state, blank))

    def test_unknown_operation_reported_on_start(self):
        self.project.operation("intent", "op1", "fixture action not performed")
        result = self.invoke("SessionStart")["hookSpecificOutput"]["additionalContext"]
        self.assertIn('"unknown_operations": 1', result)


if __name__ == "__main__":
    unittest.main()
