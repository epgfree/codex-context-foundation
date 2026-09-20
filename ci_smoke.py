"""Build, install, restart and disconnect only inside a disposable test profile."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import zipfile

import bundle


def run(args, cwd, stdin=""):
    result = subprocess.run(args, input=stdin, text=True, encoding="utf-8", capture_output=True,
                            timeout=45, cwd=cwd, env={**os.environ, "PYTHONUTF8": "1"})
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout


def main():
    source = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory() as td:
        base = Path(td).resolve()
        archive = base / "package.zip"
        bundle.build(source, archive)
        with zipfile.ZipFile(archive) as z:
            z.extractall(base / "extracted")
        package = base / "extracted/codex-context-foundation"
        destination = base / "Установка with spaces"
        codex = base / "codex"
        codex.mkdir()
        original = b'model = "preserved"\r\n# untouched\r\n'
        (codex / "config.toml").write_bytes(original)
        installer = [sys.executable, "-X", "utf8", str(package / "install.py")]
        flags = ["--destination", str(destination), "--codex-dir", str(codex)]
        run(installer + ["verify"], base)
        result = json.loads(run(installer + ["install", *flags, "--activate", "--pilot"], base))
        assert result["activated"] and result["hook_trust"] == "user_review_required"
        doctor = json.loads(run(installer + ["doctor", *flags], base))
        assert doctor["status"] == "local_configuration_valid", doctor
        project = base / "Проект"
        project.mkdir()
        (project / "pyproject.toml").write_text("# fixture", encoding="utf-8")
        server = tomllib.loads((codex / "config.toml").read_text(encoding="utf-8"))["mcp_servers"]["context_foundation"]
        command = [server["command"], *server["args"]]
        initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
        def request(name, arguments):
            message = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
            output = run(command, project, json.dumps(initialize) + "\n" + json.dumps(message) + "\n")
            response = json.loads(output.splitlines()[-1])
            assert "result" in response and not response["result"].get("isError"), response
            return json.loads(response["result"]["content"][0]["text"])
        request("context_record", {"kind": "decision", "title": "Решение", "body": "Сохранённый результат", "evidence": "synthetic test"})
        request("context_checkpoint", {"task": "demo", "owner": "sender", "expected_revision": 0, "goal": "41 + 1", "next_step": "compute", "summary": "synthetic", "evidence": "fixture"})
        request("context_handoff", {"action": "prepare", "task": "demo", "owner": "sender"})
        request("context_handoff", {"action": "bind", "task": "demo", "owner": "sender", "target": "receiver"})
        request("context_handoff", {"action": "claim", "task": "demo", "owner": "receiver"})
        status = request("context_status", {})
        assert status["counts"]["notes"] == 1, status
        assert request("context_search", {"query": "Сохранённый"})["documents"]
        run(installer + ["disconnect", *flags], base)
        assert (codex / "config.toml").read_bytes() == original
        assert (destination / "state").is_dir()
        print(json.dumps({"platform": sys.platform, "package": "verified", "install": "pass", "stdio_restart": "pass", "unicode_memory": "pass", "handoff": "pass", "disconnect_preserves_config": "pass"}))


if __name__ == "__main__":
    main()
