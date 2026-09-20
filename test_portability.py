"""Platform-neutral contracts plus actual Windows hook process tests."""
import base64
import json
import os
from pathlib import Path, PureWindowsPath
import subprocess
import sys
import tempfile
import tomllib
import unittest

import install
from foundation import project_key, project_root


class PortabilityTests(unittest.TestCase):
    def test_windows_config_roundtrips_unicode_and_shell_metacharacters(self):
        exe = PureWindowsPath("C:/Users/O'Brien & Co/Python/python.exe")
        module = PureWindowsPath("C:/Работа/$values;test/context_service.py")
        state = PureWindowsPath("C:/Память/with spaces")
        text = install.connection_config("", exe, module, state, platform="win32")
        config = tomllib.loads(text)
        hook = config["hooks"]["SessionStart"][0]["hooks"][0]
        self.assertEqual(hook["command"], hook["command_windows"])
        script = base64.b64decode(hook["command"].split()[-1]).decode("utf-16le")
        self.assertIn("O''Brien & Co", script)
        self.assertIn("'" + str(module.with_name("lifecycle.py")) + "'", script)
        self.assertIn("'" + str(state) + "'", script)
        self.assertNotIn("ExecutionPolicy", hook["command"])
        self.assertEqual(config["mcp_servers"][install.SERVER]["command"], str(exe))
        self.assertEqual(config["mcp_servers"][install.SERVER]["env"]["PYTHONUTF8"], "1")

    def test_crlf_unrelated_config_survives_disconnect(self):
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td).resolve()
            original = b'model = "preserve"\r\n# keep CRLF\r\n'
            config = install.connection_config(original.decode(), Path(sys.executable), directory / "context_service.py", directory / "state")
            (directory / "config.toml").write_bytes(config.encode())
            self.assertTrue(install.uninstall_connection(directory))
            self.assertEqual((directory / "config.toml").read_bytes(), original)

    def test_windows_package_paths_rejected_on_every_platform(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ["C:evil", "doc.md:stream", "a\\..\\escape", "\\\\server\\share"]:
                (root / "package-manifest.json").write_text(json.dumps({"version": install.VERSION, "files": {name: "invalid"}}), encoding="utf-8")
                with self.subTest(name=name), self.assertRaises(ValueError):
                    install.verify_package(root)

    @unittest.skipUnless(sys.platform == "win32", "requires native Windows")
    def test_windows_hook_actual_stdin_stdout_and_exit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve() / "Код с пробелами & апостроф'"
            root.mkdir()
            script = root / "lifecycle.py"
            script.write_text("import sys,json\nprint(json.dumps({'input':sys.stdin.read(),'args':sys.argv[1:]},ensure_ascii=False))\n", encoding="utf-8")
            command = install.hook_command(Path(sys.executable), root / "context_service.py", root / "Память")
            result = subprocess.run(command.split(), input="Русский ввод", text=True, encoding="utf-8", capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            response = json.loads(result.stdout)
            self.assertEqual(response["input"], "Русский ввод")
            self.assertEqual(response["args"], ["--state-dir", str(root / "Память")])

    @unittest.skipUnless(sys.platform == "win32", "requires native Windows")
    def test_windows_case_alias_same_identity(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve() / "MixedCase"
            root.mkdir()
            self.assertEqual(project_key(root), project_key(Path(str(root).swapcase())))


if __name__ == "__main__":
    unittest.main()
