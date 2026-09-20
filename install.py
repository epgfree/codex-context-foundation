"""Portable, reversible installer. No project data or credentials are imported."""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import shlex
import sys
import tempfile
import tomllib

from platform_fs import reject_linked_path, write_atomic, private_directory, default_destination

VERSION = "0.1.0-pre5"
START = "# BEGIN CODEX CONTEXT FOUNDATION (managed)"
END = "# END CODEX CONTEXT FOUNDATION (managed)"
SERVER = "context_foundation"


def hook_command(executable, module, state, platform=None):
    """Encode Windows arguments rather than interpolate paths into cmd/PowerShell."""
    args = [str(executable), "-X", "utf8", str(module.with_name("lifecycle.py")), "--state-dir", str(state)]
    if (platform or sys.platform) != "win32":
        return shlex.join(args)
    quote = lambda value: "'" + value.replace("'", "''") + "'"
    script = ("[Console]::InputEncoding = [Text.UTF8Encoding]::new($false); "
              "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
              "$OutputEncoding = [Console]::OutputEncoding; & "
              + " ".join(map(quote, args)) + "; exit $LASTEXITCODE")
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand " + encoded


def remove_block(text: str) -> str:
    if START not in text and END not in text:
        return text
    if text.count(START) != 1 or text.count(END) != 1:
        raise ValueError("Ambiguous managed markers; configuration was not changed")
    start, finish = text.index(START), text.index(END)
    if finish < start:
        raise ValueError("Invalid marker order")
    finish += len(END)
    if text[finish:finish+1] == "\n":
        finish += 1
    return text[:start] + text[finish:]


def connection_config(original: str, executable: Path, module: Path, state: Path, platform=None) -> str:
    parsed = tomllib.loads(original)
    if SERVER in parsed.get("mcp_servers", {}) and START not in original:
        raise ValueError("Existing unowned server uses the same name")
    clean = remove_block(original)
    q = json.dumps
    command = hook_command(executable, module, state, platform)
    hooks = ""
    hook_events = ("SessionStart", "PreCompact", "PostToolUse", "Stop", "SessionEnd")
    for event in hook_events:
        hooks += f'\n[[hooks.{event}]]\n'
        if event == "PostToolUse":
            hooks += 'matcher = "^apply_patch$|^Edit$|^Write$"\n'
        hooks += f'[[hooks.{event}.hooks]]\ntype = "command"\ncommand = {q(command)}\ntimeout = 3\n'
        if (platform or sys.platform) == "win32":
            hooks += f'command_windows = {q(command)}\n'
        if event == "SessionStart":
            hooks += 'additionalContextLimit = 500\n'
    block = f'''{START}
[mcp_servers.{SERVER}]
command = {q(str(executable))}
args = [{q(str(module))}, "serve", "--state-dir", {q(str(state))}]
env = {{ PYTHONUTF8 = "1", PYTHONIOENCODING = "utf-8" }}
enabled = true
required = false
startup_timeout_sec = 15
tool_timeout_sec = 60
{hooks}
{END}
'''
    result = clean + ("" if not clean or clean.endswith("\n") else "\n") + block
    after = tomllib.loads(result)
    before_other = tomllib.loads(clean)
    after_other = copy.deepcopy(after)
    after_other["mcp_servers"].pop(SERVER)
    if not after_other["mcp_servers"] and "mcp_servers" not in before_other:
        after_other.pop("mcp_servers")
    for event in hook_events:
        after_other["hooks"][event].pop()
        if not after_other["hooks"][event] and event not in before_other.get("hooks", {}):
            after_other["hooks"].pop(event)
    if not after_other["hooks"] and "hooks" not in before_other:
        after_other.pop("hooks")
    if before_other != after_other:
        raise ValueError("Unrelated configuration changed")
    return result


def verify_package(source: Path) -> dict:
    reject_linked_path(source / "package-manifest.json")
    manifest = json.loads((source / "package-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("version") != VERSION:
        raise ValueError("Unsupported package version")
    if not manifest.get("files"):
        raise ValueError("Empty package")
    for name, expected in manifest["files"].items():
        rel = Path(name)
        if (rel.is_absolute() or not name or ".." in rel.parts or "\\" in name or ":" in name
                or name == "package-manifest.json"):
            raise ValueError("Unsafe package path")
        current = source
        for part in rel.parts:
            current = current / part
            try:
                reject_linked_path(current)
            except (OSError, ValueError) as exc:
                raise ValueError("Symlink or reparse point in package") from exc
        if hashlib.sha256(current.read_bytes()).hexdigest() != expected:
            raise ValueError("Package integrity mismatch")
    return manifest


def install(source: Path, destination: Path, codex: Path, activate: bool = False, pilot: bool = False) -> dict:
    manifest = verify_package(source)
    if sys.version_info < (3, 11):
        raise ValueError("Python 3.11 or newer is required")
    if sys.platform not in manifest.get("supported_platforms", ["darwin", "linux", "win32"]):
        raise ValueError("Platform not qualified for this release")
    if pilot and not activate:
        raise ValueError("Pilot requires explicit activation")
    qualified = manifest.get("qualified_for_pilot", False) if pilot else manifest.get("qualified_for_activation", False)
    if activate and not qualified:
        raise ValueError("This pre-release is not qualified for global activation")
    destination = destination.expanduser().absolute()
    codex = codex.expanduser().absolute()
    reject_linked_path(destination)
    reject_linked_path(codex / "config.toml")
    if destination in {Path(destination.anchor), Path.home(), Path.home() / "Documents"}:
        raise ValueError("Installation requires a dedicated directory")
    private_directory(destination)
    release = destination / "releases" / VERSION
    fingerprint = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    if release.exists():
        installed = verify_package(release)
        if installed != manifest:
            raise ValueError("Release differs; use a new version instead of overwriting")
    else:
        release.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".release-", dir=release.parent))
        try:
            for name in [*manifest["files"], "package-manifest.json"]:
                target = staging / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / name, target)
            verify_package(staging)
            os.replace(staging, release)
        except BaseException:
            # Own newly created staging only; never delete previous releases/state.
            shutil.rmtree(staging)
            raise
    config_path = codex / "config.toml"
    previous = config_path.read_bytes() if config_path.exists() else b""
    new_config = None
    if activate:
        new_config = connection_config(previous.decode(), Path(sys.executable), release / "context_service.py", destination / "state")
        snapshot = destination / "private-backups" / (hashlib.sha256(previous).hexdigest() + ".toml")
        if not snapshot.exists():
            private_directory(snapshot.parent)
            write_atomic(snapshot, previous)
        mode = (config_path.stat().st_mode & 0o777) if config_path.exists() else 0o600
        if (config_path.read_bytes() if config_path.exists() else b"") != previous:
            raise ValueError("Configuration changed during installation; retry after reviewing it")
        write_atomic(config_path, new_config.encode(), mode)
    result = {"version": VERSION, "release": str(release), "fingerprint": fingerprint,
              "activated": bool(activate), "pilot": bool(pilot), "project_data_imported": False,
              "hook_trust": "user_review_required" if activate else "not_requested"}
    # Merely staging an update must not replace the active installation's record.
    if activate or not (destination / "installation.json").exists():
        write_atomic(destination / "installation.json", (json.dumps(result, indent=2) + "\n").encode())
    return result


def uninstall_connection(codex: Path) -> bool:
    path = codex / "config.toml"
    reject_linked_path(path)
    if not path.exists():
        return False
    original = path.read_bytes().decode("utf-8")
    new = remove_block(original)
    tomllib.loads(new)
    if new == original:
        return False
    if path.read_bytes().decode("utf-8") != original:
        raise ValueError("Configuration changed during disconnect; no replacement performed")
    write_atomic(path, new.encode(), path.stat().st_mode & 0o777)
    return True


def diagnose(destination: Path, codex: Path) -> dict:
    """Read-only local deployment checks, never return configuration or credentials."""
    issues = []
    try:
        reject_linked_path(destination / "installation.json")
        metadata = json.loads((destination / "installation.json").read_text(encoding="utf-8"))
        release = Path(metadata["release"])
        if release != destination / "releases" / VERSION:
            issues.append("installed_release_differs_from_this_diagnostic")
        else:
            verify_package(release)
    except (OSError, ValueError, KeyError):
        return {"status": "needs_attention", "issues": ["installation_or_integrity_check_failed"], "normal_tools_available": True}
    try:
        path = codex / "config.toml"
        reject_linked_path(path)
        config = tomllib.loads(path.read_text(encoding="utf-8"))
        server = config.get("mcp_servers", {}).get(SERVER, {})
        expected_args = [str(release / "context_service.py"), "serve", "--state-dir", str(destination / "state")]
        if not server.get("enabled", False) or server.get("args") != expected_args:
            issues.append("mcp_connection_missing_disabled_or_changed")
        executable = server.get("command", "")
        if not executable or not Path(executable).is_absolute() or not os.access(executable, os.X_OK):
            issues.append("python_runtime_unavailable")
        expected_command = hook_command(executable, release / "lifecycle.py", destination / "state")
        for name in ("SessionStart", "PreCompact", "PostToolUse", "Stop", "SessionEnd"):
            matches = sum(h.get("command_windows", h.get("command")) == expected_command
                          if sys.platform == "win32" else h.get("command") == expected_command
                          for group in config.get("hooks", {}).get(name, []) for h in group.get("hooks", []))
            if matches != 1:
                issues.append(f"hook_{name}_missing_or_duplicated")
        if config.get("features", {}).get("hooks") is False:
            issues.append("hooks_disabled_by_user")
    except (OSError, ValueError, TypeError, AttributeError):
        issues.append("configuration_check_failed")
    return {"status": "needs_attention" if issues else "local_configuration_valid",
            "issues": issues, "hook_trust": "requires_host_check", "live_mcp": "not_tested",
            "normal_tools_available": True, "configuration_changed": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "verify", "doctor", "disconnect"])
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--destination", type=Path, default=default_destination())
    parser.add_argument("--codex-dir", type=Path, default=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))))
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--pilot", action="store_true", help="Activate a locally qualified experimental release; host verification and hook trust still required")
    args = parser.parse_args()
    try:
        if args.action == "verify":
            result = {"package": "verified", "version": verify_package(args.source)["version"]}
        elif args.action == "doctor":
            result = diagnose(args.destination.expanduser().absolute(), args.codex_dir.expanduser().absolute())
        elif args.action == "disconnect":
            result = {"disconnected": uninstall_connection(args.codex_dir), "project_data_deleted": False}
        else:
            result = install(args.source, args.destination, args.codex_dir, args.activate, args.pilot)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"Installation stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
