"""Portable regressions plus real Windows API tests; never emulate Windows PASS."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import platform_fs as fs
from context_service import Project, Scope


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        (self.root / "pyproject.toml").touch()
        (self.root / "docs").mkdir()


class PortableTests(Fixture):
    def test_atomic_replace_and_create_only_unicode(self):
        target = self.root / "docs/решение.md"
        fs.create_file(target, "первый выбор".encode())
        self.assertEqual(fs.read_file(target, 100), "первый выбор".encode())
        with self.assertRaises(FileExistsError):
            fs.create_file(target, b"overwrite")
        self.assertEqual(fs.read_file(target, 100), "первый выбор".encode())
        fs.write_atomic(target, b"replacement")
        self.assertEqual(fs.read_file(target, 100), b"replacement")
        self.assertEqual(list(target.parent.glob(".foundation-*")), [])

    def test_hardlinks_neither_read_nor_replaced(self):
        source = self.base / "source"
        source.write_bytes(b"untouched")
        target = self.root / "docs/hard.md"
        os.link(source, target)
        for operation in (lambda: fs.read_file(target, 100),
                          lambda: fs.write_atomic(target, b"replacement"),
                          lambda: fs.reject_linked_path(target)):
            with self.assertRaises(ValueError):
                operation()
        self.assertEqual(source.read_bytes(), b"untouched")
        self.assertEqual(target.read_bytes(), b"untouched")

    def test_private_identity_and_database_sidecars(self):
        project = Project(self.root, self.base / "state")
        for suffix in ("", "-wal", "-shm", "-journal"):
            with self.subTest(suffix=suffix):
                name = project.database.with_name(project.database.name + suffix)
                if not suffix:
                    link = self.base / "linked-db"
                    os.link(name, link)
                else:
                    link = name
                    os.link(project.database, link)
                try:
                    with self.assertRaisesRegex(ValueError, "database or sidecar"):
                        project.status()
                finally:
                    link.unlink()
        self.assertEqual(project.status()["counts"]["notes"], 0)

    def test_text_limits_and_normalization_escapes(self):
        scope = Scope(self.root)
        for name in ("docs/../README.md", "docs/./page.md", "docs//page.md", "docs\\page.md",
                     "docs/page.md:stream", "C:/docs/page.md", "/docs/page.md"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                scope.read(name)
        target = self.root / "docs/page.md"
        fs.create_file(target, b"0123456789")
        with self.assertRaisesRegex(ValueError, "[Oo]versized"):
            fs.read_file(target, 9)
        fs.write_atomic(target, b"contains\0nul")
        with self.assertRaisesRegex(ValueError, "text-size"):
            scope.read("docs/page.md")

    def test_windows_lexical_policy_without_claiming_native_execution(self):
        bad = [r"C:\docs\..\x.md", r"C:\docs\x.md:secret", r"C:\docs\NUL.md",
               r"C:\docs\COM1.txt", r"C:\docs\LPT².md", r"C:\docs\CONIN$",
               "C:\\docs\\file. ", r"C:\docs\file.", r"C:\docs\*.md",
               r"\\server\share\file", r"\\?\C:\docs\file", r"\\.\NUL",
               r"C:docs\file", r"\docs\file", r"C:\docs\file" + "\0"]
        for name in bad:
            with self.subTest(name=name), self.assertRaises(ValueError):
                fs._windows_parts(name)
        self.assertEqual(fs._windows_parts(r"C:\Users\имя\file.md")[1][-1], "file.md")

    @unittest.skipIf(fs.WINDOWS, "POSIX descriptor and mode regression")
    def test_posix_links_fifo_owner_modes_and_pinned_directory(self):
        outside = self.base / "outside"
        outside.mkdir()
        link = self.root / "docs/link"
        link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            fs.create_file(link / "new.md", b"no")
        with self.assertRaises(ValueError):
            fs.reject_linked_path(link / "missing")
        self.assertEqual(list(outside.iterdir()), [])
        fifo = self.root / "docs/fifo.md"
        os.mkfifo(fifo)
        with self.assertRaises(ValueError):
            fs.read_file(fifo, 100)
        private, identity = fs.private_directory(self.base / "private")
        with patch.object(fs.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaisesRegex(ValueError, "user-owned"):
                fs.private_directory(private)
        private.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "private"):
            fs.private_directory(private)
        private.chmod(0o700)
        with fs.directory_handle(private) as fd:
            private.rename(self.base / "old-private")
            private.mkdir(mode=0o700)
            self.assertEqual(fs._handle_identity(fd), identity)
            self.assertNotEqual(fs.private_directory(private)[1], identity)


@unittest.skipUnless(sys.platform == "win32" and fs.WINDOWS, "requires native Windows APIs and NTFS (not WSL)")
class NativeWindowsTests(Fixture):
    """Actual handles/ACLs/junctions. Failures on a Windows runner are not skipped.

    Only the explicit privilege-dependent foreign-owner setup may be skipped;
    the owner comparison test still exercises GetSecurityInfo without privileges.
    """

    def test_native_rename_abi_and_pinned_parent_create_replace_collision(self):
        C = fs.C
        pointer = C.sizeof(fs.W.HANDLE)
        self.assertEqual(fs._Rename.root.offset, pointer)
        self.assertEqual(fs._Rename.length.offset, 2 * pointer)
        self.assertEqual(fs._Rename.name.offset, 2 * pointer + 4)
        self.assertEqual(C.sizeof(fs.W.WCHAR), 2)
        original_nt, original_win32 = fs._ntsetinfo, fs._setinfo
        target = self.root / "docs/решение-😀.md"
        seen = []

        def native_rename(handle, iosb, buffer, length, kind):
            self.assertEqual(kind, 10)
            info = fs._Rename.from_buffer(buffer)
            self.assertTrue(info.root)
            self.assertEqual(fs._identity(info.root), parent_identity)
            encoded = C.string_at(C.addressof(buffer) + fs._Rename.name.offset, info.length)
            self.assertEqual(encoded, target.name.encode("utf-16-le"))
            self.assertGreaterEqual(length, fs._Rename.name.offset + info.length)
            self.assertGreaterEqual(length, C.sizeof(fs._Rename))
            # Every ancestor remains pinned through the actual native call.
            for directory in (target.parent, self.root):
                with self.assertRaises(OSError):
                    directory.rename(directory.with_name(directory.name + "-moved"))
            seen.append(bool(info.replace))
            return original_nt(handle, iosb, buffer, length, kind)

        def win32_cleanup_only(handle, kind, buffer, length):
            self.assertEqual(kind, 4, "rename must not use the Win32 wrapper")
            return original_win32(handle, kind, buffer, length)

        with fs.directory_handle(target.parent, writing=True) as parent:
            parent_identity = fs._identity(parent)
        with patch.object(fs, "_ntsetinfo", side_effect=native_rename), \
                patch.object(fs, "_setinfo", side_effect=win32_cleanup_only):
            fs.create_file(target, b"")  # Same operation used for a new SQLite DB.
            self.assertEqual(fs.read_file(target, 100), b"")
            with self.assertRaises(FileExistsError):
                fs.create_file(target, b"must not replace")
            self.assertEqual(fs.read_file(target, 100), b"")
            fs.write_atomic(target, "новые данные".encode())
            self.assertEqual(fs.read_file(target, 100), "новые данные".encode())
        self.assertEqual(seen, [False, False, True])
        self.assertEqual(list(target.parent.glob(".foundation-*")), [])

    def test_native_rename_failure_preserves_target_and_removes_temporary(self):
        target = self.root / "docs/page.md"
        fs.create_file(target, b"original")
        # STATUS_INVALID_PARAMETER must become WinError 87, not a false success
        # or an error inferred from kernel32's unrelated last-error slot.
        status = fs.W.LONG(0xC000000D).value
        with patch.object(fs, "_ntsetinfo", return_value=status):
            with self.assertRaises(OSError) as raised:
                fs.write_atomic(target, b"replacement")
        self.assertEqual(raised.exception.winerror, 87)
        self.assertIn("0xC000000D", " ".join(raised.exception.__notes__))
        self.assertEqual(fs.read_file(target, 100), b"original")
        self.assertEqual(list(target.parent.glob(".foundation-*")), [])
        missing = target.with_name("missing.md")
        with patch.object(fs, "_ntsetinfo", return_value=status):
            with self.assertRaises(OSError):
                fs.create_file(missing, b"must not publish partially")
        self.assertFalse(missing.exists())
        self.assertEqual(list(target.parent.glob(".foundation-*")), [])

    def test_native_rename_single_character_name_relative_to_parent(self):
        # A one-WCHAR name exercises trailing struct padding. A different name
        # in the project root catches accidental use of the wrong directory.
        sibling = self.root / "a"
        sibling.write_bytes(b"leave alone")
        target = self.root / "docs/a"
        fs.create_file(target, b"first")
        with self.assertRaises(FileExistsError):
            fs.create_file(target, b"second")
        self.assertEqual(fs.read_file(target, 100), b"first")
        fs.write_atomic(target, b"replacement")
        self.assertEqual(fs.read_file(target, 100), b"replacement")
        self.assertEqual(sibling.read_bytes(), b"leave alone")
        self.assertEqual(list(target.parent.glob(".foundation-*")), [])

    def junction(self, path, target):
        run = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(path), str(target)],
                             capture_output=True, timeout=10)
        self.assertEqual(run.returncode, 0, repr(run.stderr))
        self.addCleanup(lambda: os.rmdir(path) if path.exists() else None)

    def set_security(self, path, sddl, owner=False):
        C, W = fs.C, fs.W
        sd = C.c_void_p()
        fs._ok(fs._sddl(sddl, 1, C.byref(sd), None))
        setter = fs._bind(fs._security, "SetFileSecurityW", [W.LPCWSTR, W.DWORD, C.c_void_p], W.BOOL)
        try:
            fs._ok(setter(str(path), 1 if owner else 0x80000004, sd))
        finally:
            fs._localfree(sd)

    def owner_of_handle(self, handle):
        C = fs.C
        owner, sd = C.c_void_p(), C.c_void_p()
        result = fs._getsecurity(handle, 1, 1, C.byref(owner), None, None, None, C.byref(sd))
        if result:
            raise C.WinError(result)
        try:
            return fs._sid_text(owner)
        finally:
            fs._localfree(sd)

    def token_default_owner(self):
        token = fs.W.HANDLE()
        fs._ok(fs._opentoken(fs._process(), 0x0008, fs.C.byref(token)))
        try:
            owner = fs._token_buffer(token, 4)
            return fs._sid_text(fs.C.c_void_p.from_buffer(owner).value)
        finally:
            fs._close(token)

    def test_private_acl_created_and_inherited_by_real_sqlite_sidecars(self):
        default_owner = self.token_default_owner()
        project = Project(self.root, self.base / "private-state")
        with fs.directory_handle(project.state) as h:
            fs._check_acl(h)
        with project.db() as db:
            db.execute("INSERT INTO meta VALUES ('unicode', 'данные')")
            for suffix in ("", "-wal", "-shm"):
                path = project.database.with_name(project.database.name + suffix)
                self.assertTrue(path.exists(), suffix)
                with fs.directory_handle(path.parent) as parent:
                    h = fs._open_child(parent, path.name, directory=False, private=True, sharing=7,
                                       allow_token_owner=bool(suffix))
                    try:
                        self.assertEqual(self.owner_of_handle(h), default_owner if suffix else fs._user_sid())
                    finally:
                        fs._close(h)
        self.assertEqual(Project(self.root, self.base / "private-state").status()["counts"]["notes"], 0)
        self.assertEqual(self.token_default_owner(), default_owner, "process token must not change")

    def test_elevated_default_owner_requires_all_token_evidence(self):
        # Native SID structures, but controlled token query results: exercise
        # negative policy cases even when CI always runs with an elevated token.
        C, W = fs.C, fs.W
        convert = fs._bind(fs._security, "ConvertStringSidToSidW",
                           [W.LPCWSTR, C.POINTER(C.c_void_p)], W.BOOL)
        user, admin, other = fs._user_sid(), "S-1-5-32-544", "S-1-5-32-545"
        pointers = {}
        for sid in (user, admin, other):
            ptr = C.c_void_p()
            fs._ok(convert(sid, C.byref(ptr)))
            pointers[sid] = ptr.value
            self.addCleanup(fs._localfree, ptr)

        def sid_buffer(sid):
            buf = C.create_string_buffer(C.sizeof(fs._SidAttributes))
            C.c_void_p.from_buffer(buf).value = pointers[sid]
            return buf

        cases = [
            (user, admin, 1, admin, 0x0C, admin),  # elevated owner-capable admin
            (user, admin, 0, admin, 0x0C, None),  # non-elevated
            (user, user, 1, admin, 0x0C, None),   # admin is not TokenOwner
            (user, other, 1, other, 0x0C, None),  # arbitrary default-owner group
            (user, admin, 1, admin, 0x04, None),  # enabled, not owner-capable
            (user, admin, 1, admin, 0x08, None),  # owner-capable, not enabled
            (user, admin, 1, admin, 0x1C, None),  # deny-only fails closed
            (user, admin, 1, other, 0x0C, None),  # admin group absent
            (other, admin, 1, admin, 0x0C, None), # different token user
        ]
        for token_user, owner, elevated, group_sid, flags, expected in cases:
            with self.subTest(owner=owner, elevated=elevated, flags=flags, group=group_sid, user=token_user):
                groups = C.create_string_buffer(C.sizeof(fs._TokenGroups))
                C.cast(groups, C.POINTER(fs._TokenGroups)).contents.count = 1
                group = fs._SidAttributes.from_buffer(groups, fs._TokenGroups.groups.offset)
                group.sid, group.attributes = pointers[group_sid], flags
                elevation = C.create_string_buffer(C.sizeof(W.DWORD))
                W.DWORD.from_buffer(elevation).value = elevated
                values = {1: sid_buffer(token_user), 4: sid_buffer(owner), 20: elevation, 2: groups}
                with patch.object(fs, "_token_buffer", side_effect=lambda token, kind: values[kind]):
                    self.assertEqual(fs._elevated_default_owner(user), expected)

    def test_real_foreign_system_file_owner_is_not_a_sidecar_owner(self):
        # Read only the security descriptor of an OS-owned file. This exercises
        # a genuine foreign owner without enabling privileges or changing tokens.
        path = Path(os.environ["SystemRoot"]) / "System32/ntdll.dll"
        h = fs._create(str(path), 0x00020080, 7, None, 3, 0x00200000, None)
        if h == fs.W.HANDLE(-1).value:
            raise fs.C.WinError(fs.C.get_last_error())
        try:
            owner = self.owner_of_handle(h)
            if owner in (fs._user_sid(), "S-1-5-32-544"):
                self.skipTest("OS fixture has no foreign owner on this Windows image")
            # Even an arbitrary default-owner return must not allow a foreign SID.
            with patch.object(fs, "_elevated_default_owner", return_value=owner):
                with self.assertRaisesRegex(ValueError, "foreign owner"):
                    fs._check_acl(h, allow_token_owner=True)
        finally:
            fs._close(h)

    def test_default_owner_exception_does_not_allow_admin_acl_or_main_database(self):
        project = Project(self.root, self.base / "private-state")
        user = fs._user_sid()
        sidecar = project.database.with_name(project.database.name + "-journal")
        # Normal CreateFile creation uses the actual TokenOwner on this runner.
        sidecar.write_bytes(b"")
        try:
            with fs.database_guard(project.database, project.state_identity):
                pass
            self.set_security(sidecar, f"D:P(A;;FA;;;{user})(A;;FR;;;BA)")
            with self.assertRaisesRegex(ValueError, "database or sidecar"):
                with fs.database_guard(project.database, project.state_identity):
                    pass
        finally:
            sidecar.unlink()
        # When the actual default owner differs, an identically created main DB
        # must still be rejected. The normal-token case is covered above too.
        if self.token_default_owner() != user:
            main = project.state / "other.sqlite3"
            main.write_bytes(b"")
            with self.assertRaisesRegex(ValueError, "database or sidecar"):
                with fs.database_guard(main, project.state_identity):
                    pass

    def test_broad_and_null_dacl_rejected_without_repair(self):
        user = fs._user_sid()
        for dacl in (f"D:P(A;OICI;FA;;;{user})(A;OICI;FR;;;WD)", "D:NO_ACCESS_CONTROL",
                     f"D:P(A;OICI;FA;;;{user})(A;OIIO;FR;;;BU)"):
            with self.subTest(dacl=dacl):
                path, _ = fs.private_directory(self.base / ("private-" + str(len(dacl))))
                self.set_security(path, dacl)
                with self.assertRaisesRegex(ValueError, "private|DACL"):
                    fs.private_directory(path)
                # Repeat proves there was no silent chmod/ACL repair.
                with self.assertRaises(ValueError):
                    fs.private_directory(path)

    def test_foreign_owner_comparison_uses_native_security_descriptor(self):
        path, _ = fs.private_directory(self.base / "private")
        with fs.directory_handle(path) as h, patch.object(fs, "_user_sid", return_value="S-1-5-21-1-2-3-9999"):
            with self.assertRaisesRegex(ValueError, "foreign owner"):
                fs._check_acl(h)

    def test_noninheritable_directory_acl_refused_before_sidecar_creation(self):
        path, _ = fs.private_directory(self.base / "private")
        self.set_security(path, f"D:P(A;;FA;;;{fs._user_sid()})")
        with self.assertRaisesRegex(ValueError, "inheritable"):
            fs.private_directory(path)
        with self.assertRaises(ValueError):
            Project(self.root, path)
        self.assertEqual(list(path.iterdir()), [])

    def test_actual_foreign_owner_when_token_can_assign_it(self):
        path, _ = fs.private_directory(self.base / "foreign-owner")
        user = fs._user_sid()
        try:
            self.set_security(path, "O:BA", owner=True)
        except OSError as exc:
            if exc.winerror in (5, 1307, 1314):
                self.skipTest("token cannot assign a foreign owner; native descriptor comparison tested separately")
            raise
        try:
            with self.assertRaisesRegex(ValueError, "foreign owner"):
                fs.private_directory(path)
        finally:
            self.set_security(path, "O:" + user, owner=True)

    def test_broad_acl_on_each_sqlite_file_rejected_before_connect(self):
        project = Project(self.root, self.base / "private-state")
        user = fs._user_sid()
        for suffix in ("", "-wal", "-shm", "-journal"):
            path = project.database.with_name(project.database.name + suffix)
            if suffix:
                fs.create_file(path, b"", 0o600)
            self.set_security(path, f"D:P(A;;FA;;;{user})(A;;FR;;;WD)")
            try:
                with patch("context_service.sqlite3.connect") as connect:
                    with self.assertRaisesRegex(ValueError, "database or sidecar"):
                        project.status()
                    connect.assert_not_called()
            finally:
                self.set_security(path, f"D:P(A;;FA;;;{user})")
                if suffix:
                    path.unlink()

    def test_junctions_at_every_component_and_late_swap(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret.md").write_bytes(b"EXTERNAL_SENTINEL")
        scope = Scope(self.root)
        junction = self.root / "docs/linked"
        self.junction(junction, outside)
        for path in (junction / "secret.md", junction / "missing/new.md"):
            with self.assertRaises(ValueError):
                fs.reject_linked_path(path)
        with self.assertRaises((OSError, ValueError)):
            scope.read("docs/linked/secret.md")
        with self.assertRaises((OSError, ValueError)):
            scope.create_document("docs/linked/new.md", "must not write")
        self.assertNotIn("docs/linked/secret.md", list(scope.files()))
        self.assertFalse((outside / "new.md").exists())
        linked_root = self.base / "linked-root"
        self.junction(linked_root, self.root)
        with self.assertRaises(ValueError):
            Scope(linked_root)
        with self.assertRaises((OSError, ValueError)):
            fs.private_directory(linked_root / "state")

    def test_pinned_ancestors_cannot_be_renamed_during_document_read(self):
        target = self.root / "docs/page.md"
        fs.create_file(target, b"safe")
        original_open = fs._open_child
        attempts = []

        def race(parent, name, **kwargs):
            if name == "page.md":
                for directory in (self.root, self.root / "docs"):
                    with self.assertRaises(OSError):
                        directory.rename(directory.with_name(directory.name + "-moved"))
                    attempts.append(directory)
            return original_open(parent, name, **kwargs)

        with patch.object(fs, "_open_child", side_effect=race):
            self.assertEqual(fs.read_file(target, 100), b"safe")
        self.assertEqual(len(attempts), 2)

    def test_sqlite_directory_pinned_for_entire_connection(self):
        project = Project(self.root, self.base / "private-state")
        with project.db():
            for directory in (project.state, project.state.parent):
                with self.assertRaises(OSError):
                    directory.rename(directory.with_name(directory.name + "-moved"))
        old = project.state.with_name("old-state")
        project.state.rename(old)
        fs.private_directory(project.state)
        with self.assertRaisesRegex(ValueError, "replaced"):
            project.status()

    def test_symlink_leaf_refused_when_privilege_available(self):
        external = self.base / "external"
        external.write_bytes(b"untouched")
        path = self.root / "docs/link.md"
        try:
            path.symlink_to(external)
        except OSError as exc:
            if exc.winerror == 1314:
                self.skipTest("Windows symlink privilege unavailable; junction tests remain mandatory")
            raise
        for action in (lambda: fs.read_file(path, 100), lambda: fs.create_file(path, b"new"),
                       lambda: fs.write_atomic(path, b"new")):
            with self.assertRaises((OSError, ValueError)):
                action()
        self.assertEqual(external.read_bytes(), b"untouched")

    def test_windows_native_paths_reject_ads_devices_and_namespaces(self):
        scope = Scope(self.root)
        for relative in ("docs/NUL.md", "docs/CON.txt.md", "docs/a.md:stream", "docs/LPT¹.md",
                         "docs/COM9.md", "docs/a. /page.md", "docs/a?.md"):
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                scope.create_document(relative, "no")
        for path in ("\\\\?\\" + str(self.root), "\\\\.\\C:\\", r"\\localhost\C$\Windows", "C:relative"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                fs.reject_linked_path(path)
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.base)}):
            self.assertEqual(fs.default_destination(), self.base / "codex-context-foundation")
        with patch.dict(os.environ, {"LOCALAPPDATA": r"\\server\share"}):
            with self.assertRaises(ValueError):
                fs.default_destination()

    def test_utf8_stdio_on_non_utf8_python_configuration(self):
        root = self.base / "проект"
        root.mkdir()
        (root / "pyproject.toml").touch()
        request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        status = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "context_status", "arguments": {}}}
        run = subprocess.run([sys.executable, str(Path(__file__).with_name("context_service.py")),
                              "serve", "--state-dir", str(self.base / "состояние")], cwd=root,
                             input=(json.dumps(request) + "\n" + json.dumps(status) + "\n").encode(),
                             capture_output=True, timeout=20,
                             env={**os.environ, "PYTHONUTF8": "0", "PYTHONIOENCODING": "cp1252"})
        self.assertEqual(run.returncode, 0, repr(run.stderr))
        replies = [json.loads(line) for line in run.stdout.decode("utf-8").splitlines()]
        payload = json.loads(replies[-1]["result"]["content"][0]["text"])
        self.assertEqual(payload["project"], str(root))


if __name__ == "__main__":
    unittest.main()
