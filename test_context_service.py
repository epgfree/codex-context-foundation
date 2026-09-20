import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from context_service import Project, Scope, validate_arguments
from platform_fs import private_directory


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        (self.root / "pyproject.toml").touch()
        (self.root / "docs").mkdir()
        (self.root / "src").mkdir()
        self.state = self.base / "state"
        self.project = Project(self.root, self.state)

    def checkpoint(self, owner="first", revision=0):
        return self.project.checkpoint("task", owner, revision, "goal", "next", "summary", "source and test evidence")

    def test_search_updates_after_edit_and_removal(self):
        doc = self.root / "docs/decision.md"
        doc.write_text("alpha design reason")
        self.assertEqual(len(self.project.search("alpha")["documents"]), 1)
        doc.write_text("beta changed decision")
        self.assertEqual(self.project.search("alpha")["documents"], [])
        self.assertEqual(len(self.project.search("beta")["documents"]), 1)
        doc.unlink()
        self.assertEqual(self.project.search("beta")["documents"], [])

    def test_private_files_not_indexed(self):
        (self.root / "private").mkdir()
        (self.root / "private/secret.md").write_text("PRIVATE_SENTINEL")
        self.assertEqual(self.project.search("PRIVATE_SENTINEL")["documents"], [])

    def test_edit_between_index_and_excerpt_does_not_return_stale_text(self):
        doc = self.root / "docs/decision.md"
        doc.write_text("alpha old decision")
        refresh = self.project.refresh

        def changed_after_scan():
            result = refresh()
            doc.write_text("beta new decision")
            return result

        with patch.object(self.project, "refresh", changed_after_scan):
            result = self.project.search("alpha")
        self.assertEqual(result["documents"], [])
        self.assertTrue(result["coverage"]["changed_during_query"])
        self.assertEqual(len(self.project.search("beta")["documents"]), 1)

    def test_late_file_symlink_rejected_and_cache_removed(self):
        doc = self.root / "docs/page.md"
        doc.write_text("public value")
        self.project.search("public")
        secret = self.base / "secret.md"
        secret.write_text("PRIVATE_SENTINEL")
        doc.unlink()
        doc.symlink_to(secret)
        self.assertEqual(self.project.search("PRIVATE_SENTINEL")["documents"], [])
        self.assertEqual(self.project.search("public")["documents"], [])
        with self.assertRaises(OSError):
            self.project.scope.read("docs/page.md")

    def test_parent_symlink_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("PRIVATE_SENTINEL")
        (self.root / "docs/link").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            self.project.scope.read("docs/link/secret.md")

    def test_code_read_rejects_late_link(self):
        outside = self.base / "secret.py"
        outside.write_text("PRIVATE_SENTINEL = 1")
        (self.root / "src/link.py").symlink_to(outside)
        self.assertEqual(self.project.code("PRIVATE_SENTINEL")["matches"], [])

    def test_traversal_and_hidden_files_rejected(self):
        for path in ["../outside.md", "/tmp/outside.md", "docs/.env.md", "docs/private/a.md"]:
            with self.assertRaises(ValueError):
                self.project.scope.read(path)

    def test_hardlink_rejected(self):
        outside = self.base / "secret.md"
        outside.write_text("PRIVATE_SENTINEL")
        (self.root / "docs/hard.md").hardlink_to(outside)
        with self.assertRaises(ValueError):
            self.project.scope.read("docs/hard.md")

    def test_two_same_named_projects_isolated(self):
        other = self.base / "elsewhere/project"
        other.mkdir(parents=True)
        (other / "pyproject.toml").touch()
        second = Project(other, self.state)
        self.project.record("decision", "alpha", "unique choice", "source verified")
        self.assertEqual(second.search("unique")["notes"], [])
        self.assertNotEqual(self.project.database, second.database)

    def test_linked_state_parent_rejected(self):
        outside = self.base / "outside-state"
        outside.mkdir(mode=0o700)
        linked = self.base / "linked-state"
        linked.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            Project(self.root, linked / "nested")
        self.assertEqual(list(outside.iterdir()), [])

    @unittest.skipIf(sys.platform == "win32", "POSIX mode test; Windows ACL tests are in test_platform_fs")
    def test_world_readable_state_not_silently_reused(self):
        public = self.base / "public-state"
        public.mkdir(mode=0o755)
        public.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "private"):
            Project(self.root, public)

    def test_late_sqlite_sidecar_link_rejected(self):
        external = self.base / "outside"
        external.write_text("untouched")
        sidecar = self.project.database.with_name("context.sqlite3-wal")
        sidecar.symlink_to(external)
        with self.assertRaisesRegex(ValueError, "sidecar"):
            self.project.status()
        self.assertEqual(external.read_text(), "untouched")

    def test_hardlinked_database_rejected(self):
        (self.base / "database-link").hardlink_to(self.project.database)
        with self.assertRaisesRegex(ValueError, "database"):
            self.project.status()

    def test_state_directory_replacement_rejected(self):
        self.project.state.rename(self.base / "saved-original-state")
        # Keep permissions valid so this tests identity replacement on both
        # POSIX and Windows, not an unrelated inherited Windows ACL failure.
        private_directory(self.project.state)
        with self.assertRaisesRegex(ValueError, "replaced"):
            self.project.status()

    def test_record_idempotent_and_restart_persistent(self):
        one = self.project.record("decision", "alpha", "choice", "test")
        two = self.project.record("decision", "alpha", "choice", "test")
        self.assertEqual(one["id"], two["id"])
        restarted = Project(self.root, self.state)
        self.assertEqual(restarted.status()["counts"]["notes"], 1)
        self.assertEqual((self.root / one["path"]).read_text().count("# alpha"), 1)
        self.assertEqual(len(restarted.search("choice")["documents"]), 1)
        self.assertEqual(restarted.search("choice")["notes"], [])

    def test_record_uses_existing_wiki_and_preserves_pages(self):
        (self.root / "wiki").mkdir()
        old = self.root / "wiki/overview.md"
        old.write_text("original page")
        saved = self.project.record("decision", "alpha", "choice", "test")
        self.assertTrue(saved["path"].startswith("wiki/"))
        self.assertEqual(old.read_text(), "original page")
        self.assertFalse((self.root / "docs/decisions").exists())

    def test_multiple_wikis_require_explicit_routing(self):
        (self.root / "wiki").mkdir()
        (self.root / "knowledge").mkdir()
        with self.assertRaisesRegex(ValueError, "canonical"):
            self.project.record("decision", "alpha", "choice", "test")
        saved = self.project.record("decision", "alpha", "choice", "test", "knowledge/decisions")
        self.assertTrue(saved["path"].startswith("knowledge/decisions/"))

    def test_changed_canonical_record_is_not_resurrected_from_cache(self):
        saved = self.project.record("decision", "alpha", "obsoletevalue", "test")
        file = self.root / saved["path"]
        file.write_text("replacementvalue")
        self.assertEqual(self.project.search("obsoletevalue")["documents"], [])
        self.assertEqual(self.project.search("obsoletevalue")["notes"], [])
        self.assertEqual(len(self.project.search("replacementvalue")["documents"]), 1)
        with self.assertRaisesRegex(ValueError, "changed"):
            self.project.record("decision", "alpha", "obsoletevalue", "test")
        self.assertEqual(file.read_text(), "replacementvalue")
        file.unlink()
        with self.assertRaises(FileNotFoundError):
            self.project.record("decision", "alpha", "obsoletevalue", "test")
        self.assertFalse(file.exists())

    def test_record_retry_after_file_publish_before_database_commit(self):
        saved = self.project.record("decision", "alpha", "choice", "test")
        with self.project.db() as db:
            db.execute("DELETE FROM note_sources")
            db.execute("DELETE FROM notes")
        repeated = self.project.record("decision", "alpha", "choice", "test")
        self.assertEqual(repeated["path"], saved["path"])
        self.assertEqual(len(list((self.root / "docs/decisions").glob("*.md"))), 1)

    def test_record_rejects_linked_parent_and_unsafe_roots(self):
        outside = self.base / "outside"
        outside.mkdir()
        (self.root / "wiki").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            self.project.record("decision", "alpha", "choice", "test")
        self.assertEqual(list(outside.iterdir()), [])
        for directory in ["../outside", "/tmp", "src", "docs/../src", "docs/.private"]:
            with self.subTest(directory=directory), self.assertRaises(ValueError):
                self.project.record("decision", "alpha", "choice", "test", directory)

    def test_create_document_never_overwrites_links_or_different_content(self):
        file = self.root / "docs/existing.md"
        file.write_text("human text")
        with self.assertRaisesRegex(ValueError, "differs"):
            self.project.scope.create_document("docs/existing.md", "replacement")
        self.assertEqual(file.read_text(), "human text")
        (self.root / "docs/linked.md").symlink_to(file)
        with self.assertRaises(OSError):
            self.project.scope.create_document("docs/linked.md", "human text")
        self.assertEqual(list((self.root / "docs").glob(".foundation-*")), [])

    def test_legacy_database_note_remains_searchable(self):
        with self.project.db() as db:
            db.execute("INSERT INTO notes VALUES ('old','decision','oldtitle','legacyvalue','evidence',1)")
        result = self.project.search("legacyvalue")
        self.assertEqual(result["notes"][0]["storage"], "legacy_database_note")
        self.assertEqual(result["documents"], [])

    def test_checkpoint_revision_and_owner(self):
        self.assertEqual(self.checkpoint()["revision"], 1)
        with self.assertRaisesRegex(ValueError, "revision"):
            self.checkpoint()
        with self.assertRaisesRegex(ValueError, "another"):
            self.checkpoint("second", 1)
        self.assertEqual(self.checkpoint("first", 1)["revision"], 2)

    def test_handoff_new_owner_and_old_owner_blocked(self):
        self.checkpoint()
        self.project.handoff("prepare", "task", "first")
        with self.assertRaises(ValueError):
            self.checkpoint("first", 1)
        self.project.handoff("bind", "task", "first", "second")
        with self.assertRaises(ValueError):
            self.project.handoff("claim", "task", "third")
        self.project.handoff("claim", "task", "second")
        with self.assertRaises(ValueError):
            self.checkpoint("first", 1)
        self.assertEqual(self.checkpoint("second", 1)["revision"], 2)

    def test_pending_client_id_not_accepted(self):
        self.checkpoint()
        self.project.handoff("prepare", "task", "first")
        with self.assertRaises(ValueError):
            self.project.handoff("bind", "task", "first", "client-new-thread:pending")

    def test_unknown_outcome_blocks_new_actions(self):
        self.project.operation("intent", "one", "one action")
        with self.assertRaisesRegex(ValueError, "unknown"):
            self.project.operation("intent", "two", "another action")
        self.project.operation("resolve", "one", result="applied", evidence="verified local evidence")
        self.project.operation("intent", "two", "another action")

    def test_resolved_key_cannot_be_reused(self):
        self.project.operation("intent", "one", "action")
        self.project.operation("resolve", "one", result="not_applied", evidence="verified no action")
        with self.assertRaisesRegex(ValueError, "already used"):
            self.project.operation("intent", "one", "action")

    def test_existing_canonical_journal_not_replaced(self):
        (self.root / ".promotion").mkdir()
        (self.root / ".promotion/journal.sqlite3").touch()
        with self.assertRaisesRegex(ValueError, "canonical"):
            self.project.operation("intent", "one", "action")

    def test_git_worktree_separates_search_but_detects_canonical_journal(self):
        def git(*args):
            return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True, text=True).stdout.strip()
        git("init", "-q")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-qm", "fixture")
        worktree = self.base / "worktree"
        git("worktree", "add", "--detach", str(worktree))
        (self.root / ".promotion").mkdir()
        (self.root / ".promotion/journal.sqlite3").touch()
        project = Project(worktree, self.state)
        self.assertEqual(project.repository_root, self.root)
        self.assertNotEqual(project.database, self.project.database)
        with self.assertRaisesRegex(ValueError, "canonical project"):
            project.operation("intent", "one", "action")
        self.assertEqual(project.status()["counts"]["operations"], 0)
        (self.root / "docs/only-main.md").write_text("mainonlysentinel")
        self.assertEqual(project.search("mainonlysentinel")["documents"], [])

    def test_unresolved_git_identity_blocks_second_journal(self):
        (self.root / ".git").write_text("gitdir: /missing/fixture/path\n")
        project = Project(self.root, self.state)
        self.assertTrue(project.status()["git_identity_error"])
        with self.assertRaisesRegex(ValueError, "identity unresolved"):
            project.operation("intent", "one", "action")

    def test_status_exposes_bounded_handoff_directory_without_full_context(self):
        for n in range(12):
            self.project.checkpoint(f"task{n}", "first", 0, "PRIVATE_GOAL", "next", "PRIVATE_SUMMARY", "evidence")
        status = self.project.status()
        self.assertEqual(len(status["tasks"]), 10)
        self.assertTrue(status["tasks_limited"])
        self.assertNotIn("PRIVATE_", json.dumps(status))

    def test_stdio_initialize_catalog_and_status(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "fixture", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "context_status", "arguments": {}}},
        ]
        run = subprocess.run([sys.executable, str(Path(__file__).with_name("context_service.py")), "serve", "--state-dir", str(self.state)], cwd=self.root,
                             input="".join(json.dumps(x) + "\n" for x in requests), capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        responses = [json.loads(x) for x in run.stdout.splitlines()]
        self.assertEqual(len(responses), 3)
        self.assertEqual({tool["name"] for tool in responses[1]["result"]["tools"]}, {
            "context_status", "context_search", "context_read", "context_code", "context_record",
            "context_checkpoint", "context_handoff", "context_operation"})
        status = json.loads(responses[2]["result"]["content"][0]["text"])
        self.assertEqual(status["project"], str(self.root))

    def test_tool_schema_validation(self):
        for name, values in [("context_status", {"extra": 1}), ("context_search", {}),
                             ("context_search", {"query": "a", "limit": True}),
                             ("context_search", {"query": "a", "limit": "5"}),
                             ("context_search", {"query": 123}), ("missing", {})]:
            with self.subTest(name=name, values=values), self.assertRaises(ValueError):
                validate_arguments(name, values)


if __name__ == "__main__":
    unittest.main()
