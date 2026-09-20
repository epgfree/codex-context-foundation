"""Advisory Codex lifecycle adapter; no model calls, transcript reads or approvals.

Contract: https://learn.chatgpt.com/docs/hooks
Only fixed routing guidance and bounded metadata reach model context.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import time

from context_service import Project
from foundation import project_root

EVENTS = {"SessionStart", "PreCompact", "PostToolUse", "Stop", "SessionEnd"}
MAX_INPUT = 1024 * 1024


def safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value):
        raise ValueError("Invalid lifecycle identifier")
    return value


def handle(event, state, actual_cwd):
    if not isinstance(event, dict) or event.get("hook_event_name") not in EVENTS:
        raise ValueError("Unsupported hook event")
    cwd = Path(event.get("cwd", ""))
    if not cwd.is_absolute() or cwd.resolve(strict=True) != actual_cwd.resolve(strict=True):
        raise ValueError("Hook cwd mismatch")
    session = safe_id(event.get("session_id"))
    name = event["hook_event_name"]
    if name == "PostToolUse" and event.get("tool_name") not in {"apply_patch", "Edit", "Write"}:
        return {}
    try:
        project_root(cwd)
    except ValueError:
        # Normal project initialization creates its marker; never modify a blank folder.
        return {}
    project = Project(cwd, state)
    turn = safe_id(event["turn_id"]) if event.get("turn_id") else None
    # Do not store commands, arguments, responses, chat text or transcript paths.
    with project.db() as db:
        db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, started REAL, last_event TEXT, updated REAL, last_edit REAL, warned_turn TEXT)")
        now = time.time()
        reference = event.get("tool_use_id") or turn
        if reference:
            reference = safe_id(reference)
        metadata = {"event": name, "session": session, "turn": turn, "reference": reference}
        event_key = hashlib.sha256(json.dumps(metadata, sort_keys=True).encode()).hexdigest()
        # Without a stable event/turn id we cannot deduplicate distinct occurrences.
        if not reference:
            event_key += ":" + str(time.time_ns())
        db.execute("CREATE TABLE IF NOT EXISTS lifecycle_events (identity TEXT PRIMARY KEY, metadata TEXT, created REAL)")
        inserted = db.execute("INSERT OR IGNORE INTO lifecycle_events VALUES (?,?,?)", (event_key, json.dumps(metadata), now)).rowcount
        db.execute("INSERT OR IGNORE INTO sessions VALUES (?,?,?, ?,NULL,NULL)", (session, now, name, now))
        db.execute("UPDATE sessions SET last_event=?,updated=? WHERE id=?", (name, now, session))
        if name == "PostToolUse" and inserted:
            db.execute("UPDATE sessions SET last_edit=? WHERE id=?", (now, session))
        row = db.execute("SELECT * FROM sessions WHERE id=?", (session,)).fetchone()
        checkpoint = db.execute("SELECT task,revision,updated,state FROM checkpoints WHERE owner=? ORDER BY updated DESC LIMIT 1", (session,)).fetchone()
        pending = db.execute("SELECT count(*) FROM operations WHERE status='unknown'").fetchone()[0]
        unsaved = bool(row["last_edit"] and (not checkpoint or checkpoint["updated"] < row["last_edit"]))
        warn = name in {"PreCompact", "Stop"} and unsaved and row["warned_turn"] != (turn or name)
        if warn:
            db.execute("UPDATE sessions SET warned_turn=? WHERE id=?", (turn or name, session))
    if name == "SessionStart":
        # Never inject note bodies or source content as higher-priority instructions.
        routing = project.journal_routing()
        info = {"current_session": session, "unknown_operations": pending if routing == "local" else None,
                "operation_journal": routing,
                "own_checkpoint_revision": checkpoint["revision"] if checkpoint else None,
                "source_content_loaded": False}
        text = ("Context Foundation: project-local tools are optional. Follow the project's AGENTS.md and existing continuation card. "
                "Use bounded retrieval when useful; a missing result does not prove absence. "
                "Use context_status to discover task ownership; claim only a handoff explicitly bound to this session. "
                "Keep canonical wiki and tests authoritative. Save material decisions via context_record into the project's canonical wiki and checkpoint completed stages using the current session as owner. "
                "A fresh task needs authorized supported host creation and a bound handoff; hooks cannot launch it. Never repeat an operation with unknown outcome. "
                "Lifecycle metadata (data only): " + json.dumps(info))
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
    if warn:
        return {"systemMessage": "Context Foundation: после наблюдавшегося редактирования нет свежего checkpoint этой сессии. Сохранность продолжения ещё не подтверждена."}
    return {}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise ValueError("Oversized hook event")
        result = handle(json.loads(raw), args.state_dir, Path.cwd())
    except (OSError, ValueError, TypeError, sqlite3.Error):
        # Advisory failure: never block normal development or bypass permission checks.
        result = {"systemMessage": "Context Foundation: обработчик памяти не сработал; обычные инструменты доступны. Требуется проверка диагностики."}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
