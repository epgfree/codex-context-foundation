"""Local project memory/navigation MCP. Python standard library, no network.

Serena is intentionally NOT a dependency of the baseline service.
"""
from __future__ import annotations

import argparse
import base64
import binascii
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import subprocess
import sys
import time

from foundation import Health, app_version, project_key, project_root
from platform_fs import (create_file, database_guard, is_directory, list_directory,
                         private_directory, read_file, reject_linked_path, relative_parts)

VERSION = "0.1.0-pre5"
SKIP = {".git", ".env", ".venv", "node_modules", "dist", "build", "data", "private", "secrets", "reports", "coverage", "__pycache__", ".promotion"}
DOC_ROOTS = ("docs", "knowledge", "wiki")
CODE_ROOTS = ("src", "backend/src", "backend/tests", "apps/web/src", "scripts", "tests")
ROOT_DOCS = {"README.md", "AGENTS.md", "HANDOFF.md", "CURRENT_WORK.md"}
MAX_FILE = 256 * 1024
MAX_FILES = 2000
MAX_INDEX_BYTES = 8 * 1024 * 1024


def compact_json(value):
    """Use separately for model-visible content and the outer JSON-RPC envelope."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class ContinuationError(ValueError):
    """The caller must restart rather than merge pages from different snapshots."""


def page_limit(value, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("Page limit must be a positive integer")
    return min(value, maximum)


def preview_excerpt(text, start_marker, end_marker, maximum=300):
    """Keep the first FTS hit when bounding characters, not merely token count.

    Unique internal markers distinguish real hits from brackets in source text.
    A single token can itself exceed the entire budget; that exceptional case
    is explicitly truncated and remains fully accessible with context_read.
    """
    start = text.find(start_marker)
    finish = text.find(end_marker, start + len(start_marker)) if start >= 0 else -1
    rendered = text.replace(start_marker, "[").replace(end_marker, "]")
    if len(rendered) <= maximum:
        return rendered, False
    budget = maximum - 2  # Reserve both boundary ellipses.
    if start >= 0 and finish >= 0:
        end = finish - len(start_marker) + 2
        if end - start > budget:
            return rendered[start:start + maximum - 1] + "…", True
        low = max(0, start - 60, end - budget)
    else:
        low = 0
    high = min(len(rendered), low + budget)
    return ("…" if low else "") + rendered[low:high] + ("…" if high < len(rendered) else ""), True


def bounded(value, name, maximum=4000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name}: non-empty string, maximum {maximum} characters")
    return value.strip()


class Scope:
    """Descriptor-based traversal: reject symlinks at every component on every read."""
    def __init__(self, root: Path):
        reject_linked_path(root)
        self.root = root.absolute()

    def permitted(self, relative: str, code=False) -> bool:
        try:
            relative_parts(relative)
        except ValueError:
            return False
        p = PurePosixPath(relative)
        if p.is_absolute() or not p.parts or any(x in {"..", "."} or x.casefold() in SKIP or x.startswith(".") for x in p.parts):
            return False
        if not code:
            return relative in ROOT_DOCS or (p.parts[0] in DOC_ROOTS and p.suffix.lower() == ".md")
        return (p.suffix.lower() in {".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go"}
                and any(relative.startswith(prefix + "/") for prefix in CODE_ROOTS))

    def create_document(self, relative, body):
        """Publish a complete new document without ever replacing an existing file.

        A failed publication may leave empty directories, never a partial document.
        Descriptor traversal rejects links; it is not a sandbox against our own UID.
        """
        parts = PurePosixPath(relative).parts
        if not self.permitted(relative) or parts[0] not in DOC_ROOTS:
            raise ValueError("Only approved documentation directories can be written")
        data = body.encode("utf-8")
        if len(data) > MAX_FILE or b"\0" in data:
            raise ValueError("Document outside text-size policy")
        try:
            create_file(self.root.joinpath(*parts), data)
        except FileExistsError:
            if self.read(relative) != body:
                raise ValueError("Existing document differs; preserve and review it")

    def read(self, relative: str, code=False) -> str:
        if not self.permitted(relative, code):
            raise ValueError("Path outside allowed source scope")
        data = read_file(self.root.joinpath(*relative_parts(relative)), MAX_FILE)
        if b"\0" in data:
            raise ValueError("File outside text-size policy")
        return data.decode("utf-8")

    def files(self, code=False):
        seen = set()
        if not code:
            for name in sorted(ROOT_DOCS):
                if (self.root / name).is_file():
                    seen.add(name)
                    yield name
        for prefix in CODE_ROOTS if code else DOC_ROOTS:
            base = self.root / prefix
            if not is_directory(base):
                continue
            pending = [base]
            while pending:
                directory = pending.pop()
                try:
                    names = sorted(list_directory(directory))
                except (OSError, ValueError):
                    continue
                for name in names:
                    if name.casefold() in SKIP or name.startswith("."):
                        continue
                    if is_directory(directory / name):
                        pending.append(directory / name)
                        continue
                    relative = (Path(directory) / name).relative_to(self.root).as_posix()
                    if relative not in seen and self.permitted(relative, code):
                        seen.add(relative)
                        yield relative


class Project:
    def __init__(self, root: Path, state: Path):
        reject_linked_path(root)
        self.root = project_root(root)
        self.scope = Scope(self.root)
        self.repository_root, self.git_identity_error = self.repository_identity()
        state, _ = private_directory(state)
        self.state, self.state_identity = private_directory(state / project_key(self.root))
        self.database = self.state / "context.sqlite3"
        try:
            create_file(self.database, b"", mode=0o600)
        except FileExistsError:
            pass
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(path UNINDEXED, body);
                CREATE TABLE IF NOT EXISTS hashes (path TEXT PRIMARY KEY, digest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS notes (id TEXT PRIMARY KEY, kind TEXT, title TEXT, body TEXT, evidence TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS note_sources (id TEXT PRIMARY KEY, path TEXT NOT NULL, digest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS checkpoints (task TEXT PRIMARY KEY, revision INTEGER, goal TEXT, next_step TEXT, summary TEXT, evidence TEXT, owner TEXT, state TEXT, target TEXT, updated REAL);
                CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY, kind TEXT, reference TEXT, payload TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS operations (key TEXT PRIMARY KEY, description TEXT, status TEXT, evidence TEXT, created REAL);
            ''')
            saved = db.execute("SELECT value FROM meta WHERE key='root'").fetchone()
            if saved and saved[0] != str(self.root):
                raise ValueError("Project identity mismatch")
            db.execute("INSERT OR IGNORE INTO meta VALUES ('root', ?)", (str(self.root),))
            # Persist only cursor authentication material, never a result cache.
            # Existing pre4 data/schema and record identities remain unchanged.
            db.execute("INSERT OR IGNORE INTO meta VALUES ('_cf_cursor_key_v1', ?)", (os.urandom(32).hex(),))
            self._cursor_key = bytes.fromhex(db.execute("SELECT value FROM meta WHERE key='_cf_cursor_key_v1'").fetchone()[0])
            if len(self._cursor_key) != 32:
                raise ValueError("Invalid cursor authentication state")

    def repository_identity(self):
        """Resolve only Git metadata, never run hooks or read another worktree's code.

        Shared identity is diagnostic/routing information; search remains worktree-local.
        """
        if not any((p / ".git").exists() or (p / ".git").is_symlink() for p in (self.root, *self.root.parents)):
            return self.root, False
        environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        environment.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", LC_ALL="C")
        try:
            result = subprocess.run(["git", "-C", str(self.root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                    capture_output=True, text=True, encoding="utf-8", timeout=3, env=environment)
            common = Path(result.stdout.strip())
            if result.returncode or not common.is_absolute() or not common.is_dir() or common.name != ".git":
                return None, True
            return common.resolve().parent, False
        except (OSError, subprocess.TimeoutExpired):
            return None, True

    @contextmanager
    def db(self):
        with database_guard(self.database, self.state_identity) as audit:
            db = sqlite3.connect(self.database, timeout=10)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA temp_store=MEMORY")
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA busy_timeout=10000")
                db.execute("BEGIN IMMEDIATE")
                audit()
                yield db
                audit()
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()
            audit()

    def refresh(self):
        found = {}
        skipped = 0
        capped = False
        indexed_bytes = 0
        for number, path in enumerate(self.scope.files()):
            if number >= MAX_FILES:
                capped = True
                break
            try:
                body = self.scope.read(path)
            except (OSError, ValueError, UnicodeError):
                skipped += 1
                continue
            encoded = body.encode()
            indexed_bytes += len(encoded)
            if indexed_bytes > MAX_INDEX_BYTES:
                capped = True
                break
            found[path] = (hashlib.sha256(encoded).hexdigest(), body)
        changed = 0
        with self.db() as db:
            old = dict(db.execute("SELECT path,digest FROM hashes"))
            for path, (digest, body) in found.items():
                if old.get(path) != digest:
                    db.execute("DELETE FROM docs WHERE path=?", (path,))
                    db.execute("INSERT INTO docs(path,body) VALUES (?,?)", (path, body))
                    db.execute("INSERT OR REPLACE INTO hashes VALUES (?,?)", (path, digest))
                    changed += 1
            # On partial scan no stale content may be returned as current.
            for path in old.keys() - found.keys():
                db.execute("DELETE FROM docs WHERE path=?", (path,))
                db.execute("DELETE FROM hashes WHERE path=?", (path,))
        return {"indexed_files": len(found), "updated_files": changed, "skipped_files": skipped, "coverage_limited": capped, "byte_budget": MAX_INDEX_BYTES}

    def status(self):
        with self.db() as db:
            counts = {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("notes", "checkpoints", "operations")}
            pending = db.execute("SELECT count(*) FROM operations WHERE status='unknown'").fetchone()[0]
            tasks = [dict(row) for row in db.execute("SELECT task,revision,owner,state,target FROM checkpoints ORDER BY updated DESC LIMIT 10")]
        routing = self.journal_routing()
        return {"project": str(self.root), "service": VERSION, "counts": counts, "unknown_operations": pending,
                "operation_journal": routing, "unknown_operations_scope": "this_service_only_not_external_journal",
                "tasks": tasks, "tasks_limited": counts["checkpoints"] > len(tasks),
                "repository_root": str(self.repository_root) if self.repository_root else None,
                "git_identity_error": self.git_identity_error,
                "serena": "disabled_unqualified", "project_data_imported": False,
                "automatic_thread_creation": "host_agent_required", "scope": {"docs": DOC_ROOTS, "code": CODE_ROOTS}}

    def _cursor(self, kind, binding, fingerprint, offset):
        # 42-byte payload + 16-byte MAC => 78 ASCII characters. No paths, bodies,
        # or raw query text travel in a cursor. The key survives service restarts.
        data = b"\x01" + kind.encode("ascii") + offset.to_bytes(8, "big") + bytes.fromhex(fingerprint)
        context = compact_json([str(self.root), kind, binding]).encode("utf-8")
        tag = hmac.digest(self._cursor_key, context + data, "sha256")[:16]
        return base64.urlsafe_b64encode(data + tag).decode("ascii").rstrip("=")

    def _continuation(self, cursor, kind, binding):
        try:
            if not isinstance(cursor, str) or len(cursor) != 78:
                raise ValueError
            raw = base64.b64decode(cursor + "==", altchars=b"-_", validate=True)
            data, tag = raw[:-16], raw[-16:]
            context = compact_json([str(self.root), kind, binding]).encode("utf-8")
            if (len(data) != 42 or data[:2] != b"\x01" + kind.encode("ascii")
                    or not hmac.compare_digest(tag, hmac.digest(self._cursor_key, context + data, "sha256")[:16])):
                raise ValueError
            return data[10:].hex(), int.from_bytes(data[2:10], "big")
        except (ValueError, TypeError, UnicodeError, binascii.Error) as exc:
            raise ContinuationError("Invalid cursor for this project/request; restart required") from exc

    def _corpus_fingerprint(self, db, coverage):
        """Hash the refreshed indexed corpus plus every legacy note, streaming rows.

        This deliberately rescans; no stat-based incremental freshness claims.
        A stable ordering is essential both here and when resolving tied ranks.
        """
        digest = hashlib.sha256()
        for row in db.execute("SELECT path,digest FROM hashes ORDER BY path COLLATE BINARY"):
            digest.update(compact_json(["d", *row]).encode("utf-8") + b"\n")
        for row in db.execute("SELECT id,kind,title,body,evidence,created FROM notes WHERE NOT EXISTS (SELECT 1 FROM note_sources WHERE note_sources.id=notes.id) ORDER BY id COLLATE BINARY"):
            digest.update(compact_json(["n", *row]).encode("utf-8") + b"\n")
        digest.update(compact_json(coverage).encode("utf-8"))
        return digest.hexdigest()

    def search(self, query: str, limit=3, cursor=None):
        words = re.findall(r"[^\W_]+", bounded(query, "query", 300), flags=re.UNICODE)
        if not words:
            raise ValueError("No searchable terms")
        limit = page_limit(limit, 10)
        binding = words
        prior, offset = self._continuation(cursor, "s", binding) if cursor is not None else (None, 0)
        refreshed = self.refresh()
        coverage = {key: refreshed[key] for key in ("coverage_limited", "skipped_files")}
        if coverage["coverage_limited"]:
            coverage.update({key: refreshed[key] for key in ("indexed_files", "byte_budget")})
        expression = " OR ".join('"' + word + '"' for word in words)
        folded = tuple(word.casefold() for word in words)
        marker = os.urandom(12).hex()
        start_marker, end_marker = f"\x01{marker}\x02", f"\x03{marker}\x04"
        with self.db() as db:
            fingerprint = self._corpus_fingerprint(db, coverage)
            if prior is not None and prior != fingerprint:
                raise ContinuationError("Corpus changed; restart search without cursor")
            # Match before LIMIT, without an arbitrary 500-row candidate cutoff.
            # Python casefold preserves the pre4 Unicode substring note semantics.
            db.create_function("cf_note_match", 2, lambda title, body: int(any(word in (title + " " + body).casefold() for word in folded)))
            count = db.execute("SELECT count(*) FROM docs WHERE docs MATCH ?", (expression,)).fetchone()[0]
            rows = db.execute("SELECT docs.path,snippet(docs,1,?,?,'…',24) AS excerpt, hashes.digest FROM docs JOIN hashes ON hashes.path=docs.path WHERE docs MATCH ? ORDER BY bm25(docs), docs.path COLLATE BINARY LIMIT ? OFFSET ?", (start_marker, end_marker, expression, limit + 1, offset)).fetchall()
            notes = []
            if len(rows) <= limit:
                notes = db.execute("SELECT id,kind,title,body,evidence FROM notes WHERE NOT EXISTS (SELECT 1 FROM note_sources WHERE note_sources.id=notes.id) AND cf_note_match(title,body) ORDER BY created DESC, id COLLATE BINARY LIMIT ? OFFSET ?", (limit + 1 - len(rows), max(0, offset - count))).fetchall()
        results = []
        for row in rows[:limit]:
            try:
                current = self.scope.read(row["path"])
                if hashlib.sha256(current.encode()).hexdigest() != row["digest"]:
                    raise ValueError("Source changed after refresh")
            except (OSError, ValueError, UnicodeError):
                # Preserve the pre4 first-page shape, but make incomplete results
                # explicit. Never advance a cursor over a failed/changed match.
                coverage["changed_during_query"] = True
                coverage["skipped_files"] += 1
                return {"documents": [], "notes": [], "coverage": coverage, "has_more": False,
                        "cursor": None, "restart_required": True, "reason": "source_changed_or_unavailable"}
            excerpt, truncated = preview_excerpt(row["excerpt"], start_marker, end_marker)
            result = {"path": row["path"], "excerpt": excerpt}
            if truncated:
                result["snippet_truncated"] = True
            results.append(result)
        matches = []
        for row in notes[:max(0, limit - len(results))]:
            note = dict(row)
            truncated = any(len(note[field]) > size for field, size in (("title", 160), ("body", 240), ("evidence", 160)))
            for field, size in (("title", 160), ("body", 240), ("evidence", 160)):
                note[field] = note[field][:size]
            note["storage"] = "legacy_database_note"
            if truncated:
                note["details_truncated"] = True  # context_read(note_id=...) recovers every field.
            matches.append(note)
        has_more = len(rows) + len(notes) > limit
        return {"documents": results, "notes": matches, "coverage": coverage,
                "has_more": has_more,
                "cursor": self._cursor("s", binding, fingerprint, offset + len(results) + len(matches)) if has_more else None}

    def read(self, path=None, note_id=None, limit=2000, cursor=None, revision=None):
        """Read Unicode character pages; concatenate text exactly, without stripping.

        Legacy note text is a complete JSON object (all persistent note fields).
        Files always use Scope.read; source safety/size limits remain unchanged.
        """
        if (path is None) == (note_id is None):
            raise ValueError("Supply exactly one of path or note_id")
        limit = page_limit(limit, 8000)
        binding = [path, note_id]
        prior, offset = self._continuation(cursor, "r", binding) if cursor is not None else (None, 0)
        if revision is not None and (not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{64}", revision)):
            raise ValueError("Revision must be a SHA-256 digest")
        try:
            if path is not None:
                path = bounded(path, "path", 1000)
                text = self.scope.read(path, code=not self.scope.permitted(path))
                reference, format_name = {"path": path}, "text"
            else:
                note_id = bounded(note_id, "note_id", 300)
                with self.db() as db:
                    row = db.execute("SELECT id,kind,title,body,evidence,created FROM notes WHERE id=? AND NOT EXISTS (SELECT 1 FROM note_sources WHERE note_sources.id=notes.id)", (note_id,)).fetchone()
                if row is None:
                    raise ValueError("Legacy note not found; canonical notes must be read by path")
                text = compact_json(dict(row))
                reference, format_name = {"note_id": note_id}, "json"
        except (OSError, ValueError, UnicodeError) as exc:
            if cursor is not None or revision is not None:
                raise ContinuationError("Source changed or unavailable; restart read") from exc
            raise
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if (prior is not None and prior != digest) or (revision is not None and revision != digest):
            raise ContinuationError("Source revision changed; restart read")
        if offset > len(text):
            raise ContinuationError("Read position no longer available; restart read")
        chunk = text[offset:offset + limit]
        end = offset + len(chunk) == len(text)
        return {**reference, "revision": digest, "format": format_name, "text": chunk,
                "next": None if end else self._cursor("r", binding, digest, offset + len(chunk)), "end": end}

    def record(self, kind, title, body, evidence, wiki_directory=None):
        if kind not in {"decision", "constraint", "regression", "research"}:
            raise ValueError("Unsupported note kind")
        title, body, evidence = bounded(title, "title", 200), bounded(body, "body", 8000), bounded(evidence, "evidence", 2000)
        identity = hashlib.sha256(json.dumps([kind, title, body, evidence], ensure_ascii=False).encode()).hexdigest()[:24]
        document = f"# {title}\n\nKind: {kind}\nRecord: {identity}\n\n{body}\n\n## Evidence\n\n{evidence}\n"
        with self.db() as db:
            old = db.execute("SELECT path,digest FROM note_sources WHERE id=?", (identity,)).fetchone()
            if old:
                # A repeated request must not undo a human edit, deletion or move.
                if self.scope.read(old["path"]) != document:
                    raise ValueError("Canonical document changed; do not restore its old version")
                path = old["path"]
            else:
                if wiki_directory is None:
                    candidates = [p for p in ("wiki", "knowledge", "docs/wiki", "docs/knowledge") if (self.root / p).exists() or (self.root / p).is_symlink()]
                    if len(candidates) > 1:
                        raise ValueError("Multiple knowledge roots: select the canonical wiki_directory from project instructions")
                    wiki_directory = candidates[0] if candidates else "docs/decisions"
                wiki_directory = bounded(wiki_directory, "wiki_directory", 200)
                path = f"{wiki_directory}/CF-{identity}.md"
                # File first: retry after a database failure safely reuses identical bytes.
                self.scope.create_document(path, document)
            db.execute("INSERT OR IGNORE INTO notes VALUES (?,?,?,?,?,?)", (identity, kind, title, body, evidence, time.time()))
            db.execute("INSERT OR IGNORE INTO note_sources VALUES (?,?,?)", (identity, path, hashlib.sha256(document.encode()).hexdigest()))
        return {"id": identity, "saved": True, "path": path, "existing_documents_overwritten": False, "note": "Canonical Markdown saved; evidence is an agent assertion, not proof of task completion"}

    def checkpoint(self, task, owner, expected_revision, goal, next_step, summary, evidence):
        task, owner = bounded(task, "task", 120), bounded(owner, "owner", 200)
        goal, next_step = bounded(goal, "goal", 2000), bounded(next_step, "next_step", 2000)
        summary, evidence = bounded(summary, "summary", 4000), bounded(evidence, "evidence", 2000)
        with self.db() as db:
            old = db.execute("SELECT * FROM checkpoints WHERE task=?", (task,)).fetchone()
            revision = old["revision"] if old else 0
            if revision != expected_revision:
                raise ValueError("Checkpoint revision conflict; reread current state")
            if old and (old["owner"] != owner or old["state"] != "active"):
                raise ValueError("Task belongs to another executor or is awaiting handoff")
            values = (task, revision+1, goal, next_step, summary, evidence, owner, "active", None, time.time())
            db.execute("INSERT OR REPLACE INTO checkpoints VALUES (?,?,?,?,?,?,?,?,?,?)", values)
            db.execute("INSERT INTO events(kind,reference,payload,created) VALUES ('checkpoint',?,?,?)", (task, json.dumps(values), time.time()))
        return {"task": task, "revision": revision+1, "saved": True, "note": "Code, tests and existing project handoff must also be saved by the agent"}

    def handoff(self, action, task, owner, target=None):
        task, owner = bounded(task, "task", 120), bounded(owner, "owner", 200)
        with self.db() as db:
            row = db.execute("SELECT * FROM checkpoints WHERE task=?", (task,)).fetchone()
            if not row:
                raise ValueError("Checkpoint required")
            if action == "read":
                return dict(row)
            if action == "prepare":
                if row["owner"] != owner or row["state"] != "active":
                    raise ValueError("Only active owner can prepare")
                db.execute("UPDATE checkpoints SET state='prepared', updated=? WHERE task=?", (time.time(), task))
            elif action == "bind":
                target = bounded(target, "target", 200)
                if target.startswith("client-new-thread:") or target == owner:
                    raise ValueError("A real different destination task is required")
                if row["owner"] != owner or row["state"] != "prepared":
                    raise ValueError("Prepared owner required")
                db.execute("UPDATE checkpoints SET state='bound',target=?,updated=? WHERE task=?", (target, time.time(), task))
            elif action == "claim":
                if row["state"] != "bound" or row["target"] != owner:
                    raise ValueError("Only bound destination can claim")
                db.execute("UPDATE checkpoints SET state='active',owner=?,target=NULL,updated=? WHERE task=?", (owner, time.time(), task))
            elif action == "cancel":
                if row["owner"] != owner or row["state"] != "prepared":
                    raise ValueError("Only an unbound handoff can be cancelled automatically")
                db.execute("UPDATE checkpoints SET state='active',updated=? WHERE task=?", (time.time(), task))
            else:
                raise ValueError("Unknown handoff action")
            db.execute("INSERT INTO events(kind,reference,payload,created) VALUES ('handoff',?,?,?)", (task, json.dumps({"action": action, "owner": owner, "target": target}), time.time()))
            return dict(db.execute("SELECT * FROM checkpoints WHERE task=?", (task,)).fetchone())

    def journal_routing(self):
        if self.git_identity_error:
            return "unresolved_git_identity"
        if any((root / ".promotion/journal.sqlite3").exists() for root in {self.root, self.repository_root} if root):
            return "canonical_project_journal"
        if self.repository_root and self.repository_root != self.root:
            return "canonical_repository_journal"
        return "local"

    def operation(self, action, key=None, description=None, result=None, evidence=None):
        routing = self.journal_routing()
        if routing == "unresolved_git_identity":
            raise ValueError("Git identity unresolved; journal routing must be checked before external actions")
        if routing == "canonical_project_journal":
            raise ValueError("Existing canonical project journal detected; use its adapter, not a second journal")
        if routing == "canonical_repository_journal":
            raise ValueError("Use the canonical repository journal for external actions; this worktree/subproject does not create another")
        with self.db() as db:
            if action == "status":
                return [dict(row) for row in db.execute("SELECT * FROM operations ORDER BY created DESC LIMIT 30")]
            key = bounded(key, "key", 300)
            if action == "intent":
                description = bounded(description, "description", 2000)
                if db.execute("SELECT 1 FROM operations WHERE status='unknown'").fetchone():
                    raise ValueError("Reconcile unknown operation before any new external action")
                if db.execute("SELECT 1 FROM operations WHERE key=?", (key,)).fetchone():
                    raise ValueError("Operation key already used; do not repeat the external action")
                db.execute("INSERT INTO operations VALUES (?,?,'unknown',NULL,?)", (key, description, time.time()))
            elif action == "resolve":
                if result not in {"applied", "not_applied", "cancelled"}:
                    raise ValueError("Unsupported confirmed result")
                evidence = bounded(evidence, "evidence", 2000)
                cursor = db.execute("UPDATE operations SET status=?,evidence=? WHERE key=? AND status='unknown'", (result, evidence, key))
                if cursor.rowcount != 1:
                    raise ValueError("No unknown operation with this key")
            else:
                raise ValueError("Unknown operation action")
            db.execute("INSERT INTO events(kind,reference,payload,created) VALUES ('operation',?,?,?)", (key, json.dumps({"action": action, "result": result, "evidence": evidence}), time.time()))
        return {"key": key, "status": "unknown" if action == "intent" else result, "external_action_performed_by_service": False}

    def code(self, name, limit=3):
        name = bounded(name, "name", 100)
        if not re.fullmatch(r"[\w.]+", name):
            raise ValueError("Symbol name only")
        matches, skipped, capped = [], 0, False
        pattern = re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)")
        for number, path in enumerate(self.scope.files(code=True)):
            if number >= MAX_FILES:
                capped = True
                break
            try:
                body = self.scope.read(path, code=True)
            except (OSError, ValueError, UnicodeError):
                skipped += 1
                continue
            for line, text in enumerate(body.splitlines(), 1):
                if pattern.search(text):
                    matches.append({"path": path, "line": line, "excerpt": text[:300]})
                    if len(matches) >= min(max(int(limit), 1), 30):
                        return {"matches": matches, "limited": True, "method": "lexical_not_LSP", "skipped": skipped}
        return {"matches": matches, "limited": capped, "method": "lexical_not_LSP", "skipped": skipped}


def schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


S = {"type": "string"}
TOOLS = [
    {"name": "context_status", "description": "Project-local memory status. No source scan or cross-project activation.", "inputSchema": schema({})},
    {"name": "context_search", "description": "Lexical search; 3 previews by default. Follow cursor with the same query; changed corpus requires restart. Use context_read for full content.", "inputSchema": schema({"query": S, "limit": {"type": "integer"}, "cursor": S}, ["query"])},
    {"name": "context_read", "description": "Read approved source path OR legacy note_id in revision-checked text pages. Follow next until end. Legacy text concatenates to JSON; source to original text.", "inputSchema": schema({"path": S, "note_id": S, "revision": S, "cursor": S, "limit": {"type": "integer"}})},
    {"name": "context_code", "description": "Literal symbol search; 3 previews by default. limited means incomplete; use normal tools to search further and context_read for full source. Not LSP.", "inputSchema": schema({"name": S, "limit": {"type": "integer"}}, ["name"])},
    {"name": "context_record", "description": "Create an evidence-linked Markdown decision/constraint/regression/research record without overwriting files. Select wiki_directory from project instructions when an existing wiki exists. Evidence is not verified by this tool.", "inputSchema": schema({k: S for k in ["kind", "title", "body", "evidence", "wiki_directory"]}, ["kind", "title", "body", "evidence"])},
    {"name": "context_checkpoint", "description": "Save compact task state using revision check. Existing owner must match; save code/tests separately.", "inputSchema": schema({**{k: S for k in ["task", "owner", "goal", "next_step", "summary", "evidence"]}, "expected_revision": {"type": "integer"}}, ["task", "owner", "goal", "next_step", "summary", "evidence", "expected_revision"])},
    {"name": "context_handoff", "description": "Read or prepare/bind/claim/cancel task handoff. Does not create Codex tasks; host agent must create destination using supported tools.", "inputSchema": schema({k: S for k in ["action", "task", "owner", "target"]}, ["action", "task", "owner"])},
    {"name": "context_operation", "description": "Record intent or resolve confirmed external outcome; rejects repeats and unknown outcomes. Never performs external actions.", "inputSchema": schema({k: S for k in ["action", "key", "description", "result", "evidence"]}, ["action"])},
]
for tool in TOOLS:
    tool["annotations"] = {"readOnlyHint": tool["name"] in {"context_status", "context_search", "context_code", "context_read"}, "destructiveHint": False, "openWorldHint": False}


def validate_arguments(name, values):
    contract = next((tool["inputSchema"] for tool in TOOLS if tool["name"] == name), None)
    if contract is None:
        raise ValueError("Unknown tool")
    if not isinstance(values, dict):
        raise ValueError("Object arguments expected")
    if set(values) - set(contract["properties"]):
        raise ValueError("Unknown argument")
    if set(contract["required"]) - set(values):
        raise ValueError("Missing required argument")
    for key, value in values.items():
        kind = contract["properties"][key]["type"]
        if kind == "string" and not isinstance(value, str):
            raise ValueError("String argument expected")
        if kind == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
            raise ValueError("Integer argument expected")


def serve(state: Path):
    project = None
    health = None
    startup_reason = "no_project_boundary"
    try:
        project = Project(Path.cwd(), state)
        health = Health(project.state / "health", project.root, app_version(Path("/Applications/ChatGPT.app/Contents/Info.plist")), notify=False)
    except (OSError, ValueError, sqlite3.Error) as exc:
        startup_reason = "no_project_boundary" if "No narrow project boundary" in str(exc) else "state_initialization_failed"
    initialized = False
    while raw := sys.stdin.buffer.readline(1024 * 1024 + 1):
        identity = None
        try:
            if len(raw) > 1024 * 1024:
                if health:
                    health.fail("oversized_client_message")
                break
            req = json.loads(raw)
            if not isinstance(req, dict):
                raise ValueError("Object request expected")
            identity = req.get("id")
            method, args = req.get("method"), req.get("params", {})
            if identity is None:
                continue
            if not isinstance(args, dict):
                raise ValueError("Object params expected")
            if method == "initialize":
                initialized = True
                requested = args.get("protocolVersion")
                version = requested if requested in {"2024-11-05", "2025-03-26", "2025-06-18"} else "2025-06-18"
                result = {"protocolVersion": version, "serverInfo": {"name": "codex-context-foundation", "version": VERSION}, "capabilities": {"tools": {}}, "instructions": "Project-local bounded retrieval and evidence-linked memory. Existing project documents and journals remain authoritative. No project import; no external actions. Handoff needs supported Codex host tools. Missing source matches are not proof of absence."}
                if health:
                    health.update("connected", handshake=True)
            elif method == "ping":
                result = {}
            elif not initialized:
                raise ValueError("Initialize first")
            elif method == "tools/list":
                result = {"tools": TOOLS}  # Stable catalog, including before a new project is initialized.
            elif method == "tools/call":
                name, values = args.get("name"), args.get("arguments", {})
                validate_arguments(name, values)
                if project is None:
                    try:
                        project = Project(Path.cwd(), state)
                        health = Health(project.state / "health", project.root,
                                        app_version(Path("/Applications/ChatGPT.app/Contents/Info.plist")), notify=False)
                        health.update("connected", handshake=True)
                    except (OSError, ValueError, sqlite3.Error) as exc:
                        startup_reason = "no_project_boundary" if "No narrow project boundary" in str(exc) else "state_initialization_failed"
                if not project:
                    if name != "context_status":
                        raise ValueError("No project boundary; normal tools remain available")
                    payload = {"status": "inactive", "reason": startup_reason, "normal_tools_available": True}
                else:
                    dispatch = {"context_status": project.status, "context_search": project.search, "context_read": project.read, "context_code": project.code, "context_record": project.record, "context_checkpoint": project.checkpoint, "context_handoff": project.handoff, "context_operation": project.operation}
                    if name not in dispatch:
                        raise ValueError("Unknown tool")
                    try:
                        payload = dispatch[name](**values)
                    except ContinuationError as exc:
                        payload = {"restart_required": True, "reason": str(exc), "cursor": None, "next": None, "end": False}
                        result = {"content": [{"type": "text", "text": compact_json(payload)}], "isError": True}
                        print(compact_json({"jsonrpc": "2.0", "id": identity, "result": result}), flush=True)
                        continue
                    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error):
                        # Tool/domain errors are MCP results, not invalid JSON-RPC requests.
                        result = {"content": [{"type": "text", "text": "Operation not completed: invalid state, denied path or unavailable storage. Read current status; do not repeat external actions automatically."}], "isError": True}
                        print(compact_json({"jsonrpc": "2.0", "id": identity, "result": result}), flush=True)
                        continue
                    if health:
                        health.update("operational", successful_tool_call=True)
                result = {"content": [{"type": "text", "text": compact_json(payload)}], "isError": False}
            else:
                response = {"jsonrpc": "2.0", "id": identity, "error": {"code": -32601, "message": "Method not found"}}
                print(compact_json(response), flush=True)
                continue
            response = {"jsonrpc": "2.0", "id": identity, "result": result}
        except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as exc:
            response = {"jsonrpc": "2.0", "id": identity, "error": {"code": -32602, "message": str(exc)[:400]}}
        print(compact_json(response), flush=True)
    if health and health.data["status"] != "failed":
        health.update("stopped")


if __name__ == "__main__":
    if os.name == "nt":
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["serve"])
    parser.add_argument("--state-dir", type=Path, required=True)
    options = parser.parse_args()
    serve(options.state_dir)
