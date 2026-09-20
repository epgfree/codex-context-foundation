"""Small STDIO supervisor; never indexes or reads project source itself."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import sys
import tempfile
import threading
import time

BASE = Path(__file__).resolve().parent
MARKERS = ("pyproject.toml", "package.json", "Cargo.toml", "go.mod", "CMakeLists.txt", "Package.swift", ".git")
READ_TOOLS = frozenset({"find_symbol", "find_referencing_symbols", "get_symbols_overview"})
MAX_LINE = 16 * 1024 * 1024


def project_root(cwd: Path, home: Path | None = None) -> Path:
    """Nearest boundary wins, including a worktree .git file; no directory scan."""
    cwd = cwd.resolve(strict=True)
    home = (home or Path.home()).resolve()
    broad = {Path(cwd.anchor), home, home / "Documents", home / "Documents/Codex", Path(tempfile.gettempdir()).resolve()}
    for candidate in (cwd, *cwd.parents):
        if candidate in broad:
            break
        if any((candidate / marker).exists() for marker in MARKERS):
            return candidate
    raise ValueError("No narrow project boundary; source navigation remains optional")


def project_key(root: Path) -> str:
    return hashlib.sha256(os.fsencode(os.path.normcase(str(root.resolve())))).hexdigest()


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def app_version(path: Path) -> dict:
    try:
        with path.open("rb") as stream:
            info = plistlib.load(stream)
        return {"version": info.get("CFBundleShortVersionString"), "build": info.get("CFBundleVersion")}
    except (OSError, plistlib.InvalidFileException, ValueError):
        return {"version": None, "build": None}


def read_json(path: Path) -> dict:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


class Health:
    def __init__(self, state: Path, root: Path, version: dict, notify: bool = True):
        self.path = state / f"health-{os.getpid()}.json"
        self.event_path = state / "last-notification.json"
        self.notify = notify
        self.lock = threading.Lock()
        self.data = {"project": str(root), "pid": os.getpid(), "app": version, "started": time.time(), "status": "starting"}
        previous = read_json(state / "last-compatible-app.json")
        self.data["app_changed_since_handshake"] = bool(previous and previous != version)
        self.compatible = state / "last-compatible-app.json"
        self.update("starting")

    def update(self, status: str, **details) -> None:
        with self.lock:
            self.data.update(status=status, updated=time.time(), **details)
            atomic_json(self.path, self.data)
            if status == "connected":
                atomic_json(self.compatible, self.data["app"])

    def fail(self, reason: str) -> None:
        # Only fixed diagnostic codes belong here, never raw tool/source output.
        self.update("failed", reason=reason)
        fingerprint = hashlib.sha256(json.dumps([reason, self.data["app"]], sort_keys=True).encode()).hexdigest()
        prior = read_json(self.event_path)
        if prior.get("fingerprint") == fingerprint and time.time() - prior.get("time", 0) < 86400:
            return
        atomic_json(self.event_path, {"fingerprint": fingerprint, "time": time.time()})
        if self.notify and sys.platform == "darwin":
            script = 'on run argv\ndisplay notification (item 1 of argv) with title "Codex: навигация по коду"\nend run'
            try:
                subprocess.run(["/usr/bin/osascript", "-e", script,
                    "Serena недоступна. Обычная работа Codex остаётся доступной; подробности в локальной диагностике."],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass


def runtime_command(manifest: dict, root: Path, state: Path) -> tuple[list[str], dict]:
    """A checked-in manifest is required; never resolve latest or install on startup."""
    executable = Path(manifest["executable"])
    if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("runtime_missing")
    for entry in manifest.get("verified_files", []):
        if hashlib.sha256(Path(entry["path"]).read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError("runtime_integrity_changed")
    if not manifest.get("version") or not manifest.get("verified_files"):
        raise ValueError("runtime_not_pinned")
    fields = {"project": str(root), "state": str(state), "base": str(BASE)}
    args = [str(executable)] + [value.format(**fields) for value in manifest["args"]]
    # Drop API credentials: this local navigation service does not need them.
    env = {key: value for key, value in os.environ.items()
           if key in {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT"}}
    env.update({key: value.format(**fields) for key, value in manifest.get("env", {}).items()})
    return args, env


class Filter:
    """MCP transport filtering is defence in depth, not an OS sandbox."""
    def __init__(self):
        self.pending = {}
        self.lock = threading.Lock()

    def request(self, value: dict) -> tuple[bool, dict | None]:
        if not isinstance(value, dict) or not isinstance(value.get("method"), str):
            return False, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
        if "params" in value and not isinstance(value["params"], dict):
            return False, {"jsonrpc": "2.0", "id": value.get("id"), "error": {"code": -32602, "message": "Expected object parameters"}}
        method = value.get("method")
        if method == "tools/call" and value.get("params", {}).get("name") not in READ_TOOLS:
            return False, {"jsonrpc": "2.0", "id": value.get("id"), "error": {"code": -32601, "message": "Only project symbol-navigation tools are enabled"}}
        if method not in {"initialize", "notifications/initialized", "ping", "tools/list", "tools/call", "notifications/cancelled"}:
            if "id" not in value:
                return False, None
            return False, {"jsonrpc": "2.0", "id": value["id"], "error": {"code": -32601, "message": "Method not enabled in navigation mode"}}
        if "id" in value:
            with self.lock:
                self.pending[value["id"]] = method
        return True, None

    def response(self, value: dict) -> tuple[dict, str | None]:
        if not isinstance(value, dict):
            raise ValueError("Invalid server message")
        with self.lock:
            method = self.pending.pop(value.get("id"), None)
        result = value.get("result")
        event = None
        if method == "tools/list" and isinstance(result, dict):
            result["tools"] = [tool for tool in result.get("tools", []) if tool.get("name") in READ_TOOLS]
        if method == "initialize" and isinstance(result, dict):
            # No resources/prompts/write APIs are available through this proxy.
            caps = result.setdefault("capabilities", {})
            result["capabilities"] = {key: val for key, val in caps.items() if key == "tools"}
            result["instructions"] = "Local symbol navigation for this session's project only. Source and tests remain authoritative. If unavailable, ordinary file search remains available."
            if result.get("protocolVersion") and result.get("serverInfo"):
                event = "connected"
        if method == "tools/call":
            event = "tool_error" if "error" in value or (isinstance(result, dict) and result.get("isError")) else "tool_ok"
        return value, event


def supervise(command: list[str], env: dict, root: Path, health: Health, timeout: float = 60) -> int:
    child = subprocess.Popen(command, cwd=root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    filter_ = Filter()
    output_lock = threading.Lock()
    ready = threading.Event()
    normal_stop = threading.Event()
    finished = threading.Event()

    def reap():
        if not finished.wait(3) and child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def stop(*_):
        already_stopping = normal_stop.is_set()
        normal_stop.set()
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if not already_stopping:
            threading.Thread(target=reap, daemon=True).start()

    def send(message):
        with output_lock:
            sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    def input_loop():
        try:
            while raw := sys.stdin.buffer.readline(MAX_LINE + 1):
                if len(raw) > MAX_LINE:
                    health.fail("oversized_client_message")
                    stop()
                    return
                value = json.loads(raw)
                forward, rejection = filter_.request(value)
                if rejection is not None:
                    send(rejection)
                if forward:
                    child.stdin.write(json.dumps(value).encode() + b"\n")
                    child.stdin.flush()
        except (ValueError, OSError, TypeError):
            if child.poll() is None:
                health.fail("client_transport_error")
        finally:
            stop()

    def output_loop():
        try:
            while raw := child.stdout.readline(MAX_LINE + 1):
                if len(raw) > MAX_LINE:
                    health.fail("oversized_server_message")
                    stop()
                    return
                parsed = json.loads(raw)
                if isinstance(parsed, dict) and "method" in parsed:
                    # Never forward sampling, elicitation or other server-initiated
                    # requests into the parent agent. This service needs none.
                    if "id" in parsed:
                        child.stdin.write(json.dumps({"jsonrpc": "2.0", "id": parsed["id"], "error": {"code": -32601, "message": "Server requests disabled"}}).encode() + b"\n")
                        child.stdin.flush()
                    continue
                value, event = filter_.response(parsed)
                if event == "connected":
                    ready.set()
                    health.update("connected", handshake=True)
                elif event == "tool_ok":
                    health.update("operational", successful_tool_call=True)
                elif event == "tool_error":
                    health.update("degraded", reason="tool_reported_error")
                send(value)
        except (ValueError, OSError, TypeError):
            if not normal_stop.is_set():
                health.fail("server_transport_error")
                stop()

    def watchdog():
        if not finished.wait(timeout) and not ready.is_set() and not normal_stop.is_set():
            health.fail("startup_timeout")
            stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    readers = [threading.Thread(target=input_loop, daemon=True), threading.Thread(target=output_loop, daemon=True)]
    for thread in readers:
        thread.start()
    threading.Thread(target=watchdog, daemon=True).start()
    code = child.wait()
    finished.set()
    readers[1].join(timeout=2)
    if not normal_stop.is_set():
        health.fail("unexpected_server_exit")
    elif health.data["status"] != "failed":
        health.update("stopped", exit_code=code)
    return 0 if normal_stop.is_set() else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["serve", "doctor"])
    parser.add_argument("--manifest", type=Path, default=BASE / "runtime.json")
    parser.add_argument("--state-dir", type=Path, default=BASE / "state")
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--no-notify", action="store_true")
    args = parser.parse_args()
    health = None
    try:
        root = project_root(args.project)
        state = args.state_dir / project_key(root)
        manifest = read_json(args.manifest)
        health = Health(state, root, app_version(Path("/Applications/ChatGPT.app/Contents/Info.plist")), not args.no_notify)
        command, env = runtime_command(manifest, root, state)
        if args.action == "doctor":
            print(json.dumps({"preflight": "pass", "project": str(root), "state": str(state), "version": manifest["version"], "handshake_tested": False}))
            health.update("preflight_pass")
            return 0
        return supervise(command, env, root, health)
    except (OSError, ValueError, KeyError) as exc:
        if health:
            health.fail(str(exc) if str(exc) in {"runtime_missing", "runtime_integrity_changed", "runtime_not_pinned"} else "preflight_failed")
        print("Context navigation unavailable; normal file tools remain usable.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
