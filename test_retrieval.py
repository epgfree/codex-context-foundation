"""Snapshot pagination, complete reads and pre4 persistent-state compatibility."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from context_service import ContinuationError, Project, compact_json


class RetrievalTests(unittest.TestCase):
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

    def document(self, name, text="orchid same ranked text"):
        target = self.root / "docs" / name
        target.write_text(text, encoding="utf-8")
        return target

    def note(self, identity, body="orchid legacy body", title="title", evidence="source", created=1):
        with self.project.db() as db:
            db.execute("INSERT INTO notes VALUES (?, 'decision', ?, ?, ?, ?)",
                       (identity, title, body, evidence, created))

    def all_hits(self, query, limit=3):
        seen, cursor, pages = [], None, []
        for _ in range(1000):
            page = self.project.search(query, limit=limit, cursor=cursor)
            pages.append(page)
            self.assertLessEqual(len(page["documents"]) + len(page["notes"]), limit)
            seen.extend(("doc", x["path"]) for x in page["documents"])
            seen.extend(("note", x["id"]) for x in page["notes"])
            self.assertEqual(page["has_more"], page["cursor"] is not None)
            if not page["has_more"]:
                return seen, pages
            cursor = page["cursor"]
        self.fail("Continuation did not terminate")

    def read_all(self, **kwargs):
        chunks, cursor, revision = [], None, None
        for _ in range(1000):
            page = self.project.read(**kwargs, cursor=cursor, revision=revision)
            revision = revision or page["revision"]
            self.assertEqual(page["revision"], revision)
            self.assertEqual(page["end"], page["next"] is None)
            self.assertLessEqual(len(page["text"]), kwargs.get("limit", 2000))
            chunks.append(page["text"])
            if page["end"]:
                return "".join(chunks)
            cursor = page["next"]
        self.fail("Read did not terminate")

    def test_tied_ranks_and_legacy_ties_have_no_gaps_or_duplicates(self):
        names = ["z.md", "é.md", "a.md", "решение.md", "b.md", "c.md", "d.md"]
        for name in names:
            self.document(name)
        for identity in ["z", "a", "c", "b"]:
            self.note(identity)
        expected = [("doc", "docs/" + name) for name in sorted(names)]
        expected += [("note", identity) for identity in ["a", "b", "c", "z"]]
        for limit in (1, 2, 3, 7, 10):
            with self.subTest(limit=limit):
                found, _ = self.all_hits("orchid", limit)
                self.assertEqual(found, expected)
                self.assertEqual(len(found), len(set(found)))

    def test_default_three_and_complete_empty_last_page(self):
        for n in range(5):
            self.document(f"{n}.md")
        first = self.project.search("orchid")
        self.assertEqual(len(first["documents"]), 3)
        self.assertTrue(first["has_more"])
        last = self.project.search("orchid", cursor=first["cursor"])
        self.assertEqual(len(last["documents"]), 2)
        self.assertFalse(last["has_more"])
        self.assertIsNone(last["cursor"])
        missing = self.project.search("unmatched")
        self.assertEqual(missing["documents"], [])
        self.assertEqual(missing["notes"], [])
        self.assertFalse(missing["has_more"])

    def test_cursor_survives_restart_replays_and_allows_changed_page_size(self):
        for n in range(7):
            self.document(f"{n}.md")
        first = self.project.search("orchid", limit=2)
        self.assertEqual(first["cursor"], self.project.search("orchid", limit=2)["cursor"])
        next_page = self.project.search("orchid", limit=3, cursor=first["cursor"])
        restarted = Project(self.root, self.state)
        self.assertEqual(next_page, restarted.search("orchid", limit=3, cursor=first["cursor"]))
        self.assertEqual([x["path"] for x in next_page["documents"]], ["docs/2.md", "docs/3.md", "docs/4.md"])

    def test_cursor_rejects_wrong_query_project_kind_and_tampering(self):
        for n in range(4):
            self.document(f"{n}.md")
        cursor = self.project.search("orchid")["cursor"]
        other_root = self.base / "other"
        other_root.mkdir()
        (other_root / "pyproject.toml").touch()
        other = Project(other_root, self.state)
        for action in (lambda: self.project.search("same", cursor=cursor),
                       lambda: other.search("orchid", cursor=cursor),
                       lambda: self.project.read(path="docs/0.md", cursor=cursor),
                       lambda: self.project.search("orchid", cursor="!" + cursor[1:]),
                       lambda: self.project.search("orchid", cursor="")):
            with self.assertRaises(ContinuationError):
                action()

    def test_edits_deletions_and_new_files_invalidate_search_cursor(self):
        paths = [self.document(f"{n}.md") for n in range(5)]
        cursor = self.project.search("orchid")["cursor"]
        paths[-1].write_text("orchid changed", encoding="utf-8")
        with self.assertRaisesRegex(ContinuationError, "Corpus changed"):
            self.project.search("orchid", cursor=cursor)
        cursor = self.project.search("orchid")["cursor"]
        paths[0].unlink()
        with self.assertRaises(ContinuationError):
            self.project.search("orchid", cursor=cursor)
        cursor = self.project.search("orchid")["cursor"]
        self.document("new.md", "unrelated corpus content")
        with self.assertRaises(ContinuationError):
            self.project.search("orchid", cursor=cursor)

    def test_legacy_updates_and_removal_invalidate_search_cursor(self):
        for n in range(4):
            self.note(str(n))
        for sql in ("UPDATE notes SET evidence='changed' WHERE id='3'", "DELETE FROM notes WHERE id='0'"):
            cursor = self.project.search("orchid", limit=1)["cursor"]
            with self.project.db() as db:
                db.execute(sql)
            with self.assertRaises(ContinuationError):
                self.project.search("orchid", cursor=cursor)

    def test_oldest_matching_legacy_note_beyond_500_is_reachable(self):
        with self.project.db() as db:
            db.executemany("INSERT INTO notes VALUES (?, 'decision', 'title', ?, 'source', ?)",
                           [(str(n), "irrelevant" if n else "orchid answer", n) for n in range(520)])
        result = self.project.search("orchid")
        self.assertEqual([x["id"] for x in result["notes"]], ["0"])
        self.assertFalse(result["has_more"])

    def test_non_ascii_document_and_casefold_legacy_answers(self):
        self.document("решение.md", "Ёлка архитектура: ответ сорок два.")
        self.note("old", "Die Straße", title="Решение")
        self.assertIn("сорок", self.project.search("архитектура")["documents"][0]["excerpt"])
        self.assertEqual(self.project.search("STRASSE")["notes"][0]["body"], "Die Straße")
        self.assertEqual(self.read_all(path="docs/решение.md", limit=7), "Ёлка архитектура: ответ сорок два.")

    def test_query_terms_are_not_silently_limited_to_twenty(self):
        self.document("last.md", "lastterm answer")
        query = " ".join([f"absent{n}" for n in range(21)] + ["lastterm"])
        self.assertEqual(self.project.search(query)["documents"][0]["path"], "docs/last.md")

    def test_canonical_note_is_not_duplicated_and_long_content_is_recovered(self):
        body = "Начало " + "полный текст 😀\n" * 350 + " финальный ответ"
        saved = self.project.record("decision", "Длинная запись", body, "evidence complete")
        result = self.project.search("финальный")
        self.assertEqual(result["notes"], [])
        self.assertEqual(result["documents"][0]["path"], saved["path"])
        self.assertEqual(self.read_all(path=saved["path"], limit=317), self.project.scope.read(saved["path"]))
        with self.assertRaisesRegex(ValueError, "canonical"):
            self.project.read(note_id=saved["id"])

    def test_long_legacy_note_every_field_survives_read_pagination(self):
        body = "orchid\n" + "данные 😀\n" * 1800 + "END"
        evidence = "evidence " * 200
        self.note("old", body, title="t" * 300, evidence=evidence)
        preview = self.project.search("orchid")["notes"][0]
        self.assertTrue(preview["details_truncated"])
        self.assertLessEqual(len(preview["body"]), 240)
        full = json.loads(self.read_all(note_id="old", limit=411))
        self.assertEqual(full, {"id": "old", "kind": "decision", "title": "t" * 300,
                                "body": body, "evidence": evidence, "created": 1.0})

    def test_matches_and_answers_beyond_previews_are_fully_recoverable(self):
        legacy = "preface " * 100 + "latekeyword answer is forty two\n" + "tail " * 200
        self.note("late", legacy)
        preview = self.project.search("latekeyword")["notes"][0]
        self.assertNotIn("latekeyword", preview["body"])
        self.assertTrue(preview["details_truncated"])
        self.assertEqual(json.loads(self.read_all(note_id=preview["id"], limit=199))["body"], legacy)
        document = "orchid " + "explanation " * 200 + "COMPLETE_ANSWER_42\n"
        self.document("long.md", document)
        hit = self.project.search("orchid")["documents"][0]
        self.assertNotIn("COMPLETE_ANSWER_42", hit["excerpt"])
        self.assertEqual(self.read_all(path=hit["path"], limit=201), document)

    def test_huge_preceding_token_does_not_remove_fts_match_from_preview(self):
        # Literal brackets must not be mistaken for the FTS match markers.
        body = "[not-a-hit] " + "x" * 2000 + " orchid answer42 " + "z" * 2000
        self.document("large-token.md", body)
        result = self.project.search("orchid")["documents"][0]
        self.assertIn("[orchid]", result["excerpt"])
        self.assertIn("answer42", result["excerpt"])
        self.assertTrue(result["snippet_truncated"])
        self.assertLessEqual(len(result["excerpt"]), 300)
        self.assertEqual(self.read_all(path=result["path"], limit=1000), body)

    def test_read_empty_file_code_and_exact_unicode_whitespace(self):
        self.document("empty.md", "")
        result = self.project.read(path="docs/empty.md")
        self.assertEqual(result["text"], "")
        self.assertTrue(result["end"])
        self.assertIsNone(result["next"])
        text = "  # café 😀\nvalue = '数据'\r\n\n "
        (self.root / "src/example.py").write_bytes(text.encode("utf-8"))
        self.assertEqual(self.read_all(path="src/example.py", limit=3), text)

    def test_read_changed_deleted_and_changed_reference_refuse_continuation(self):
        target = self.document("file.md", "0123456789")
        page = self.project.read(path="docs/file.md", limit=3)
        self.document("other.md", "0123456789")
        with self.assertRaises(ContinuationError):
            self.project.read(path="docs/other.md", cursor=page["next"])
        target.write_text("012345678x", encoding="utf-8")
        with self.assertRaises(ContinuationError):
            self.project.read(path="docs/file.md", cursor=page["next"])
        with self.assertRaises(ContinuationError):
            self.project.read(path="docs/file.md", revision=page["revision"])
        target.unlink()
        with self.assertRaises(ContinuationError):
            self.project.read(path="docs/file.md", cursor=page["next"])
        self.note("old", "0123456789")
        page = self.project.read(note_id="old", limit=3)
        with self.project.db() as db:
            db.execute("UPDATE notes SET body='updated' WHERE id='old'")
        with self.assertRaises(ContinuationError):
            self.project.read(note_id="old", cursor=page["next"])

    def test_read_scope_and_hardlink_protection_unchanged(self):
        target = self.document("file.md", "public")
        os.link(target, self.base / "hard")
        for path in ("../outside.md", "docs/../file.md", "docs/file.md", "private/secret.md"):
            with self.assertRaises((OSError, ValueError)):
                self.project.read(path=path)
        with self.assertRaises(ValueError):
            self.project.read()
        with self.assertRaises(ValueError):
            self.project.read(path="docs/file.md", note_id="old")

    def test_read_late_symlink_refuses_existing_cursor(self):
        target = self.document("file.md", "public content spanning pages")
        page = self.project.read(path="docs/file.md", limit=3)
        external = self.base / "external.md"
        external.write_text("PRIVATE_SENTINEL", encoding="utf-8")
        target.unlink()
        try:
            target.symlink_to(external)
        except OSError as exc:
            if sys.platform == "win32" and getattr(exc, "winerror", None) == 1314:
                self.skipTest("Windows symlink privilege unavailable; native junction tests are separate")
            raise
        with self.assertRaises((OSError, ValueError)):
            self.project.read(path="docs/file.md")
        with self.assertRaises(ContinuationError):
            self.project.read(path="docs/file.md", cursor=page["next"])

    def test_code_default_is_compact_and_limit_is_explicit(self):
        (self.root / "src/example.py").write_text("\n".join(f"orchid = {n}" for n in range(5)), encoding="utf-8")
        page = self.project.code("orchid")
        self.assertEqual(len(page["matches"]), 3)
        self.assertTrue(page["limited"])
        complete = self.project.code("orchid", limit=10)
        self.assertEqual(len(complete["matches"]), 5)
        self.assertFalse(complete["limited"])

    def test_race_after_refresh_never_advances_over_changed_document(self):
        target = self.document("page.md")
        refresh = self.project.refresh

        def race():
            result = refresh()
            target.write_text("changed content", encoding="utf-8")
            return result

        with patch.object(self.project, "refresh", side_effect=race):
            page = self.project.search("orchid")
        self.assertEqual(page["documents"], [])
        self.assertTrue(page["restart_required"])
        self.assertTrue(page["coverage"]["changed_during_query"])
        self.assertIsNone(page["cursor"])

    def test_coverage_caps_and_skips_remain_visible(self):
        for n in range(4):
            self.document(f"{n}.md")
        with patch("context_service.MAX_FILES", 2):
            page = self.project.search("orchid")
        self.assertTrue(page["coverage"]["coverage_limited"])
        self.assertEqual(page["coverage"]["indexed_files"], 2)
        self.assertIn("byte_budget", page["coverage"])
        self.document("nul.md", "bad\0text")
        page = self.project.search("orchid")
        self.assertEqual(page["coverage"]["skipped_files"], 1)

    def test_pre4_persistent_memory_checkpoints_and_intents_preserved(self):
        saved = self.project.record("decision", "orchid", "original", "evidence")
        self.note("old", "legacy orchid")
        self.project.checkpoint("task", "owner", 0, "goal", "next", "summary", "proof")
        self.project.operation("intent", "pending", "do once")
        tables = ("notes", "note_sources", "checkpoints", "events", "operations")
        with self.project.db() as db:
            db.execute("DELETE FROM meta WHERE key='_cf_cursor_key_v1'")
            before = {t: [tuple(r) for r in db.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in tables}
        restarted = Project(self.root, self.state)
        with restarted.db() as db:
            after = {t: [tuple(r) for r in db.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in tables}
        self.assertEqual(before, after)
        self.assertEqual(restarted.record("decision", "orchid", "original", "evidence")["id"], saved["id"])
        with self.assertRaisesRegex(ValueError, "unknown"):
            restarted.operation("intent", "again", "do twice")
        with self.assertRaisesRegex(ValueError, "another"):
            restarted.checkpoint("task", "different", 1, "goal", "next", "summary", "proof")

    def test_model_text_and_transport_are_independently_compact_utf8(self):
        self.document("пример.md", "пример ответ")
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "context_search", "arguments": {"query": "пример"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "context_read", "arguments": {"path": "docs/пример.md"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "context_search", "arguments": {"query": "пример", "cursor": "invalid"}}},
        ]
        run = subprocess.run([sys.executable, str(Path(__file__).with_name("context_service.py")),
                              "serve", "--state-dir", str(self.state)], cwd=self.root,
                             input="".join(compact_json(x) + "\n" for x in requests),
                             capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(run.returncode, 0, run.stderr)
        lines = run.stdout.splitlines()
        responses = [json.loads(line) for line in lines]
        for raw, response in zip(lines, responses):
            self.assertEqual(raw, compact_json(response))
        self.assertIn("context_read", [tool["name"] for tool in responses[1]["result"]["tools"]])
        for response in responses[2:]:
            model_text = response["result"]["content"][0]["text"]
            self.assertEqual(model_text, compact_json(json.loads(model_text)))
        self.assertIn("пример", responses[2]["result"]["content"][0]["text"])
        self.assertTrue(responses[-1]["result"]["isError"])
        self.assertTrue(json.loads(responses[-1]["result"]["content"][0]["text"])["restart_required"])


if __name__ == "__main__":
    unittest.main()
