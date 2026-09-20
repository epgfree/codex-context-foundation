import hashlib
import json
from pathlib import Path
import tempfile
import shlex
import tomllib
import unittest
from unittest.mock import patch

import install as i


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "package"
        self.source.mkdir()
        (self.source / "context_service.py").write_text("# fixture, never executed\n")
        self.manifest = {"version": i.VERSION, "qualified_for_activation": False,
                         "files": {"context_service.py": hashlib.sha256((self.source / "context_service.py").read_bytes()).hexdigest()}}
        self.save_manifest()
        self.codex = self.root / "codex"
        self.codex.mkdir()
        self.original = 'model = "preserved"\n[mcp_servers.existing]\ncommand = "untouched"\n'
        (self.codex / "config.toml").write_text(self.original)

    def save_manifest(self):
        (self.source / "package-manifest.json").write_text(json.dumps(self.manifest))

    def test_stage_install_does_not_activate_or_import(self):
        result = i.install(self.source, self.root / "another home/app with spaces", self.codex)
        self.assertFalse(result["activated"])
        self.assertFalse(result["project_data_imported"])
        self.assertEqual((self.codex / "config.toml").read_text(), self.original)

    def test_idempotent_staging(self):
        target = self.root / "install"
        a = i.install(self.source, target, self.codex)
        b = i.install(self.source, target, self.codex)
        self.assertEqual(a, b)

    def test_staging_preserves_active_installation_record(self):
        self.manifest["qualified_for_pilot"] = True
        self.save_manifest()
        target = self.root / "install"
        i.install(self.source, target, self.codex, activate=True, pilot=True)
        before = (target / "installation.json").read_bytes()
        staged = i.install(self.source, target, self.codex)
        self.assertFalse(staged["activated"])
        self.assertEqual(before, (target / "installation.json").read_bytes())

    def test_parent_symlink_is_rejected_without_changing_target(self):
        linked = self.root / "linked"
        linked.symlink_to(self.root / "elsewhere", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink|Linked"):
            i.install(self.source, linked / "install", self.codex)
        self.assertFalse((self.root / "elsewhere").exists())

    def test_concurrent_config_edit_is_preserved(self):
        self.manifest["qualified_for_pilot"] = True
        self.save_manifest()
        generate = i.connection_config
        config = self.codex / "config.toml"
        def changed(*args):
            result = generate(*args)
            config.write_text(self.original + "\n# concurrent user edit\n")
            return result
        with patch.object(i, "connection_config", changed), self.assertRaisesRegex(ValueError, "changed during"):
            i.install(self.source, self.root / "install", self.codex, activate=True, pilot=True)
        self.assertIn("concurrent user edit", config.read_text())

    def test_doctor_is_readonly_and_does_not_claim_live_connection(self):
        self.manifest["qualified_for_pilot"] = True
        self.save_manifest()
        target = self.root / "install"
        i.install(self.source, target, self.codex, activate=True, pilot=True)
        before = (self.codex / "config.toml").read_bytes()
        result = i.diagnose(target, self.codex)
        self.assertEqual(result["status"], "local_configuration_valid")
        self.assertEqual(result["live_mcp"], "not_tested")
        self.assertEqual(before, (self.codex / "config.toml").read_bytes())
        i.uninstall_connection(self.codex)
        self.assertIn("mcp_connection_missing_disabled_or_changed", i.diagnose(target, self.codex)["issues"])

    def test_activation_requires_qualification(self):
        with self.assertRaisesRegex(ValueError, "not qualified"):
            i.install(self.source, self.root / "install", self.codex, True)
        self.assertEqual((self.codex / "config.toml").read_text(), self.original)

    def test_pilot_needs_distinct_explicit_qualification(self):
        with self.assertRaises(ValueError):
            i.install(self.source, self.root / "pilot", self.codex, True, True)
        self.manifest["qualified_for_pilot"] = True
        self.save_manifest()
        result = i.install(self.source, self.root / "pilot", self.codex, True, True)
        self.assertTrue(result["pilot"])
        self.assertEqual(result["hook_trust"], "user_review_required")
        self.assertEqual(tomllib.loads((self.codex / "config.toml").read_text())["model"], "preserved")
        with self.assertRaises(ValueError):
            i.install(self.source, self.root / "pilot", self.codex, True)

    def test_integrity_failure_leaves_config(self):
        (self.source / "context_service.py").write_text("changed")
        with self.assertRaisesRegex(ValueError, "integrity"):
            i.install(self.source, self.root / "install", self.codex)
        self.assertEqual((self.codex / "config.toml").read_text(), self.original)

    def test_config_preserves_other_entries_and_is_idempotent(self):
        executable, module, state = (Path("/path with spaces") / p for p in ("python", "service.py", "state"))
        first = i.connection_config(self.original, executable, module, state)
        self.assertEqual(first, i.connection_config(first, executable, module, state))
        config = tomllib.loads(first)
        self.assertEqual(config["model"], "preserved")
        self.assertEqual(config["mcp_servers"]["existing"]["command"], "untouched")
        self.assertFalse(config["mcp_servers"][i.SERVER]["required"])

    def test_disconnect_preserves_unrelated_edits(self):
        new = i.connection_config(self.original, Path("/p"), Path("/m"), Path("/s"))
        new += '\n[extra]\nkeep = true\n'
        (self.codex / "config.toml").write_text(new)
        self.assertTrue(i.uninstall_connection(self.codex))
        self.assertTrue(tomllib.loads((self.codex / "config.toml").read_text())["extra"]["keep"])
        self.assertFalse(i.uninstall_connection(self.codex))

    def test_existing_hooks_and_disabled_feature_preserved(self):
        original = self.original + '\n[features]\nhooks = false\n[[hooks.SessionStart]]\n[[hooks.SessionStart.hooks]]\ntype = "command"\ncommand = "existing-command"\n'
        generated = i.connection_config(original, Path("/runtime/python"), Path("/release/context_service.py"), Path("/state"))
        config = tomllib.loads(generated)
        self.assertFalse(config["features"]["hooks"])
        self.assertEqual(config["hooks"]["SessionStart"][0]["hooks"][0]["command"], "existing-command")
        self.assertEqual(len(config["hooks"]["SessionStart"]), 2)
        self.assertEqual(tomllib.loads(i.remove_block(generated)), tomllib.loads(original))
        self.assertEqual(i.connection_config(generated, Path("/runtime/python"), Path("/release/context_service.py"), Path("/state")), generated)

    def test_hook_paths_quoted_and_no_trust_bypass(self):
        executable = Path("/path with space/python")
        module = Path("/release with ' quote/context_service.py")
        state = Path("/state with spaces")
        generated = i.connection_config(self.original, executable, module, state, platform="linux")
        config = tomllib.loads(generated)
        handler = config["hooks"]["SessionStart"][0]["hooks"][0]
        self.assertEqual(shlex.split(handler["command"]), [str(executable), "-X", "utf8", str(module.with_name("lifecycle.py")), "--state-dir", str(state)])
        self.assertLessEqual(handler["additionalContextLimit"], 500)
        self.assertNotIn("bypass", generated)

    def test_unowned_name_collision(self):
        text = '[mcp_servers.context_foundation]\ncommand = "user-command"\n'
        with self.assertRaisesRegex(ValueError, "unowned"):
            i.connection_config(text, Path("/p"), Path("/m"), Path("/s"))

    def test_malformed_markers(self):
        with self.assertRaises(ValueError):
            i.remove_block(i.START)
        with self.assertRaises(ValueError):
            i.remove_block(i.END + "\n" + i.START)

    def test_package_path_traversal(self):
        self.manifest["files"] = {"../outside": "bad"}
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            i.verify_package(self.source)

    def test_package_symlink_refused(self):
        file = self.source / "context_service.py"
        file.unlink()
        target = self.root / "elsewhere"
        target.write_text("# fixture, never executed\n")
        file.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "Symlink"):
            i.verify_package(self.source)


if __name__ == "__main__":
    unittest.main()
