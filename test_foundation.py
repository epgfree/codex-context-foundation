import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

import foundation as f


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def project(self, relative):
        path = self.root / relative
        path.mkdir(parents=True)
        (path / "pyproject.toml").touch()
        return path

    def test_nearest_boundary(self):
        first = self.project("outer")
        second = self.project("outer/inner")
        sub = second / "src"
        sub.mkdir()
        self.assertEqual(f.project_root(sub), second)
        self.assertNotEqual(f.project_key(first), f.project_key(second))

    def test_same_basename_isolated(self):
        first, second = self.project("a/project"), self.project("b/project")
        self.assertNotEqual(f.project_key(first), f.project_key(second))

    def test_worktree_git_file(self):
        path = self.root / "worktree"
        path.mkdir()
        (path / ".git").write_text("gitdir: /not/read/by/root/discovery")
        self.assertEqual(f.project_root(path), path)

    def test_no_boundary_no_scan(self):
        with self.assertRaises(ValueError):
            f.project_root(self.root)

    def test_home_never_activated(self):
        (self.root / "pyproject.toml").touch()
        with self.assertRaises(ValueError):
            f.project_root(self.root, home=self.root)

    def test_symlink_identity(self):
        path = self.project("original")
        link = self.root / "alias"
        link.symlink_to(path)
        self.assertEqual(f.project_key(path), f.project_key(link))

    def test_health_contains_no_source(self):
        h = f.Health(self.root / "state", self.root, {"version": "test"}, notify=False)
        h.update("connected", handshake=True)
        state = json.loads(h.path.read_text())
        self.assertTrue(state["handshake"])
        self.assertEqual(json.loads(h.compatible.read_text()), {"version": "test"})

    def test_failure_notification_deduplicated(self):
        h = f.Health(self.root, self.root, {"version": "test"}, notify=False)
        h.fail("startup_timeout")
        old = h.event_path.read_bytes()
        h.fail("startup_timeout")
        self.assertEqual(h.event_path.read_bytes(), old)

    def test_version_change_detected(self):
        first = f.Health(self.root, self.root, {"version": "a"}, notify=False)
        first.update("connected")
        second = f.Health(self.root, self.root, {"version": "b"}, notify=False)
        self.assertTrue(second.data["app_changed_since_handshake"])

    def test_tools_allowlist(self):
        p = f.Filter()
        p.request({"id": 1, "method": "tools/list"})
        reply, _ = p.response({"id": 1, "result": {"tools": [{"name": "find_symbol"}, {"name": "execute_shell_command"}, {"name": "activate_project"}]}})
        self.assertEqual([t["name"] for t in reply["result"]["tools"]], ["find_symbol"])

    def test_mutation_and_project_switch_blocked(self):
        for name in ["activate_project", "write_memory", "execute_shell_command", "replace_symbol_body"]:
            forward, rejected = f.Filter().request({"id": 2, "method": "tools/call", "params": {"name": name}})
            self.assertFalse(forward)
            self.assertEqual(rejected["error"]["code"], -32601)

    def test_read_tool_allowed(self):
        ok, rejected = f.Filter().request({"id": 2, "method": "tools/call", "params": {"name": "find_symbol"}})
        self.assertTrue(ok)
        self.assertIsNone(rejected)

    def test_initialization_contract_restricted(self):
        p = f.Filter()
        p.request({"id": 1, "method": "initialize"})
        response, event = p.response({"id": 1, "result": {"protocolVersion": "test", "serverInfo": {"name": "mock"}, "capabilities": {"tools": {}, "resources": {}}, "instructions": "unqualified upstream text"}})
        self.assertEqual(event, "connected")
        self.assertEqual(set(response["result"]["capabilities"]), {"tools"})
        self.assertNotIn("upstream", response["result"]["instructions"])

    def test_invalid_input(self):
        for bad in [None, [], {"method": "tools/call", "params": []}]:
            forward, error = f.Filter().request(bad)
            self.assertFalse(forward)
            self.assertIn("error", error)

    def test_pinned_runtime_and_minimal_environment(self):
        exe = self.root / "runtime with spaces"
        exe.write_text("placeholder")
        exe.chmod(0o700)
        manifest = {"executable": str(exe), "version": "1.0", "verified_files": [{"path": str(exe), "sha256": hashlib.sha256(exe.read_bytes()).hexdigest()}], "args": ["--project", "{project}"], "env": {"SERENA_HOME": "{state}"}}
        command, env = f.runtime_command(manifest, self.root, self.root / "state")
        self.assertEqual(command, [str(exe), "--project", str(self.root)])
        self.assertEqual(env["SERENA_HOME"], str(self.root / "state"))
        self.assertNotIn("OPENAI_API_KEY", env)
        exe.write_text("changed")
        with self.assertRaisesRegex(ValueError, "runtime_integrity_changed"):
            f.runtime_command(manifest, self.root, self.root / "state")


if __name__ == "__main__":
    unittest.main()
