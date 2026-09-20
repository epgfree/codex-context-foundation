"""Fail-closed local filesystem operations (POSIX and native Windows).

Windows uses single-component NtCreateFile opens relative to retained handles,
FILE_OPEN_REPARSE_POINT and handle metadata, never resolve/lstat as its security
boundary. Private objects require the process user's owner SID and a DACL whose
only allow trustees are that user and SYSTEM. Unknown ACEs fail closed. Newly
created private objects have a protected, inheritable user-only DACL.

Windows intentionally supports local fixed NTFS volumes only; UNC, device paths,
ADS, DOS aliases, reparse points (including cloud placeholders), and other file
systems fail closed. Administrators/SYSTEM and hostile processes running as the
same user are outside the boundary, as on POSIX. SQLite uses pathname APIs: its
private directory and ancestors stay pinned for the entire connection, with ACL
and sidecar checks before opening and before committing. No custom SQLite VFS.

API references (Microsoft Learn):
 /windows/win32/api/winternl/nf-winternl-ntcreatefile
 /windows/win32/api/winbase/ns-winbase-file_rename_info
 /windows/win32/api/aclapi/nf-aclapi-getsecurityinfo
 /windows/win32/api/securitybaseapi/nf-securitybaseapi-getace
 /windows/win32/fileio/naming-a-file
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import uuid

WINDOWS = os.name == "nt"


def _component(name: str, windows=WINDOWS):
    if not name or name in {".", ".."} or "/" in name or "\0" in name:
        raise ValueError("Unsafe path component")
    if windows:
        stem = name.split(".", 1)[0].rstrip(" ").upper()
        if (any(ord(c) < 32 or c in '\\:<>"|?*' for c in name)
                or name.endswith((".", " "))
                or stem in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$"}
                or re.fullmatch(r"(?:COM|LPT)[1-9¹²³]", stem)):
            raise ValueError("Unsafe Windows path: device, ADS or ambiguous name")
    return name


def relative_parts(relative: str):
    """Strict portable slash-separated relative name; no normalization escapes."""
    if not isinstance(relative, str) or "\\" in relative or ":" in relative:
        raise ValueError("Unsafe relative path")
    return tuple(_component(p) for p in relative.split("/"))


def _windows_parts(raw):
    raw = os.fspath(raw).replace("/", "\\")
    p = PureWindowsPath(raw)
    if not re.fullmatch(r"[A-Za-z]:", p.drive) or not p.root:
        raise ValueError("Windows requires an absolute local drive path")
    # Check the unnormalised spelling too: pathlib discards dot components.
    tail = raw[3:]
    for part in tail.split("\\") if tail else ():
        _component(part, windows=True)
    return p.anchor, p.parts[1:]


def _absolute(path):
    path = Path(path).expanduser()
    if WINDOWS:
        _windows_parts(path)  # Never turn drive-relative/device paths into local ones.
    elif ".." in path.parts:
        raise ValueError("Unsafe parent path")
    return path.absolute()


def default_destination() -> Path:
    if WINDOWS:
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            raise ValueError("LOCALAPPDATA is required on Windows")
        return _absolute(base) / "codex-context-foundation"
    return Path.home() / ".local" / "share" / "codex-context-foundation"


if WINDOWS:
    import ctypes as C
    from ctypes import wintypes as W

    class _Unicode(C.Structure):
        _fields_ = [("Length", W.USHORT), ("MaximumLength", W.USHORT), ("Buffer", W.LPWSTR)]

    class _Attributes(C.Structure):
        _fields_ = [("Length", W.ULONG), ("RootDirectory", W.HANDLE),
                    ("ObjectName", C.POINTER(_Unicode)), ("Attributes", W.ULONG),
                    ("SecurityDescriptor", C.c_void_p), ("SecurityQualityOfService", C.c_void_p)]

    class _IO(C.Structure):
        _fields_ = [("Status", C.c_void_p), ("Information", C.c_size_t)]

    class _Info(C.Structure):
        _fields_ = [("attributes", W.DWORD), ("created", W.FILETIME),
                    ("accessed", W.FILETIME), ("written", W.FILETIME),
                    ("volume", W.DWORD), ("size_high", W.DWORD), ("size_low", W.DWORD),
                    ("links", W.DWORD), ("index_high", W.DWORD), ("index_low", W.DWORD)]

    class _ACL(C.Structure):
        _fields_ = [("revision", W.BYTE), ("reserved", W.BYTE), ("size", W.WORD),
                    ("count", W.WORD), ("reserved2", W.WORD)]

    class _ACE(C.Structure):
        _fields_ = [("kind", W.BYTE), ("flags", W.BYTE), ("size", W.WORD),
                    ("mask", W.DWORD), ("sid", W.DWORD)]

    class _Rename(C.Structure):
        _fields_ = [("replace", W.DWORD), ("root", W.HANDLE),
                    ("length", W.DWORD), ("name", W.WCHAR * 1)]

    _kernel = C.WinDLL("kernel32", use_last_error=True)
    _security = C.WinDLL("advapi32", use_last_error=True)
    _ntdll = C.WinDLL("ntdll")

    def _bind(dll, name, args, result):
        f = getattr(dll, name)
        f.argtypes, f.restype = args, result
        return f

    _close = _bind(_kernel, "CloseHandle", [W.HANDLE], W.BOOL)
    _create = _bind(_kernel, "CreateFileW", [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p,
                    W.DWORD, W.DWORD, W.HANDLE], W.HANDLE)
    _info = _bind(_kernel, "GetFileInformationByHandle", [W.HANDLE, C.POINTER(_Info)], W.BOOL)
    _type = _bind(_kernel, "GetFileType", [W.HANDLE], W.DWORD)
    _drive_type = _bind(_kernel, "GetDriveTypeW", [W.LPCWSTR], W.UINT)
    _volume = _bind(_kernel, "GetVolumeInformationByHandleW", [W.HANDLE, W.LPWSTR, W.DWORD,
                    C.POINTER(W.DWORD), C.POINTER(W.DWORD), C.POINTER(W.DWORD), W.LPWSTR, W.DWORD], W.BOOL)
    _ntcreate = _bind(_ntdll, "NtCreateFile", [C.POINTER(W.HANDLE), W.DWORD, C.POINTER(_Attributes),
                    C.POINTER(_IO), C.c_void_p, W.DWORD, W.DWORD, W.DWORD, W.DWORD,
                    C.c_void_p, W.DWORD], W.LONG)
    _dos_error = _bind(_ntdll, "RtlNtStatusToDosError", [W.LONG], W.ULONG)
    _read = _bind(_kernel, "ReadFile", [W.HANDLE, C.c_void_p, W.DWORD, C.POINTER(W.DWORD), C.c_void_p], W.BOOL)
    _write = _bind(_kernel, "WriteFile", [W.HANDLE, C.c_void_p, W.DWORD, C.POINTER(W.DWORD), C.c_void_p], W.BOOL)
    _flush = _bind(_kernel, "FlushFileBuffers", [W.HANDLE], W.BOOL)
    _setinfo = _bind(_kernel, "SetFileInformationByHandle", [W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL)
    _getsecurity = _bind(_security, "GetSecurityInfo", [W.HANDLE, C.c_int, W.DWORD,
                    C.POINTER(C.c_void_p), C.c_void_p, C.POINTER(C.c_void_p), C.c_void_p,
                    C.POINTER(C.c_void_p)], W.DWORD)
    _getace = _bind(_security, "GetAce", [C.c_void_p, W.DWORD, C.POINTER(C.c_void_p)], W.BOOL)
    _validacl = _bind(_security, "IsValidAcl", [C.c_void_p], W.BOOL)
    _validsid = _bind(_security, "IsValidSid", [C.c_void_p], W.BOOL)
    _sdcontrol = _bind(_security, "GetSecurityDescriptorControl",
                       [C.c_void_p, C.POINTER(W.WORD), C.POINTER(W.DWORD)], W.BOOL)
    _sidstring = _bind(_security, "ConvertSidToStringSidW", [C.c_void_p, C.POINTER(C.c_void_p)], W.BOOL)
    _sddl = _bind(_security, "ConvertStringSecurityDescriptorToSecurityDescriptorW",
                  [W.LPCWSTR, W.DWORD, C.POINTER(C.c_void_p), C.c_void_p], W.BOOL)
    _localfree = _bind(_kernel, "LocalFree", [C.c_void_p], C.c_void_p)
    _process = _bind(_kernel, "GetCurrentProcess", [], W.HANDLE)
    _opentoken = _bind(_security, "OpenProcessToken", [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)], W.BOOL)
    _tokeninfo = _bind(_security, "GetTokenInformation", [W.HANDLE, C.c_int, C.c_void_p,
                       W.DWORD, C.POINTER(W.DWORD)], W.BOOL)

    def _ok(result):
        if not result:
            raise C.WinError(C.get_last_error())
        return result

    def _sid_text(sid):
        if not sid or not _validsid(sid):
            raise ValueError("Invalid security SID")
        text = C.c_void_p()
        _ok(_sidstring(sid, C.byref(text)))
        try:
            return C.wstring_at(text)
        finally:
            _localfree(text)

    def _user_sid():
        token = W.HANDLE()
        _ok(_opentoken(_process(), 0x0008, C.byref(token)))  # TOKEN_QUERY
        try:
            length = W.DWORD()
            _tokeninfo(token, 1, None, 0, C.byref(length))  # TokenUser
            if not length.value:
                raise C.WinError(C.get_last_error())
            buf = C.create_string_buffer(length.value)
            _ok(_tokeninfo(token, 1, buf, len(buf), C.byref(length)))
            return _sid_text(C.c_void_p.from_buffer(buf).value)
        finally:
            _close(token)

    @contextmanager
    def _private_sd():
        sid = _user_sid()
        sd = C.c_void_p()
        _ok(_sddl(f"O:{sid}D:P(A;OICI;FA;;;{sid})", 1, C.byref(sd), None))
        try:
            yield sd
        finally:
            _localfree(sd)

    def _check_acl(handle):
        owner, acl, sd = C.c_void_p(), C.c_void_p(), C.c_void_p()
        result = _getsecurity(handle, 1, 0x00000005, C.byref(owner), None,
                              C.byref(acl), None, C.byref(sd))  # SE_FILE_OBJECT, OWNER|DACL
        if result:
            raise C.WinError(result)
        try:
            user = _user_sid()
            if _sid_text(owner) != user:
                raise ValueError("Private state has a foreign owner")
            if not acl or not _validacl(acl):
                raise ValueError("Private state requires an explicit valid DACL")
            header = C.cast(acl, C.POINTER(_ACL)).contents
            inheritable_user_access = False
            for number in range(header.count):
                ptr = C.c_void_p()
                _ok(_getace(acl, number, C.byref(ptr)))
                ace = C.cast(ptr, C.POINTER(_ACE)).contents
                # Unknown/object/callback ACEs cannot be treated as harmless.
                if ace.kind not in (0, 1) or ace.size < C.sizeof(_ACE):
                    raise ValueError("Unsupported private state ACL entry")
                # Inherit-only grants still govern SQLite-created sidecars.
                if ace.kind == 0 and ace.mask:
                    trustee = _sid_text(ptr.value + _ACE.sid.offset)
                    if trustee not in (user, "S-1-5-18"):
                        raise ValueError("State must be private: broad ACL access")
                    if (trustee == user and ace.flags & 0x0F == 0x03
                            and (ace.mask & 0x001F01FF == 0x001F01FF or ace.mask & 0x10000000)):
                        inheritable_user_access = True
            if _metadata(handle).attributes & 0x10:
                control, revision = W.WORD(), W.DWORD()
                _ok(_sdcontrol(sd, C.byref(control), C.byref(revision)))
                # Prevent parent ACL propagation and default-token ACL fallback
                # when SQLite creates WAL/SHM/journal files. An after-open audit
                # alone would detect disclosure only after it had happened.
                if not control.value & 0x1000 or not inheritable_user_access:
                    raise ValueError("Private directory needs a protected, inheritable user DACL")
        finally:
            _localfree(sd)

    def _metadata(handle, directory=None):
        info = _Info()
        _ok(_info(handle, C.byref(info)))
        if _type(handle) != 1 or info.attributes & (0x400 | 0x40):  # DISK, REPARSE_POINT, DEVICE
            raise OSError(errno.ELOOP, "Reparse point or device rejected")
        is_dir = bool(info.attributes & 0x10)
        if directory is not None and is_dir != directory:
            raise ValueError("Unexpected filesystem object type")
        if not is_dir and info.links != 1:
            raise ValueError("Hard-linked file rejected")
        return info

    def _identity(handle):
        info = _metadata(handle, True)
        return info.volume, (info.index_high << 32) | info.index_low

    def _open_child(parent, name, *, directory=None, create=False, private=False,
                    write=False, sharing=1):
        _component(name, True)
        buf = C.create_unicode_buffer(name)
        length = len(name.encode("utf-16-le"))
        if length > 65532:
            raise ValueError("Path component too long")
        text = _Unicode(length, length + 2, C.cast(buf, W.LPWSTR))
        attributes = _Attributes(C.sizeof(_Attributes), parent, C.pointer(text), 0x40, None, None)
        handle, iosb = W.HANDLE(), _IO()
        access = 0x00120080  # SYNCHRONIZE | READ_CONTROL | FILE_READ_ATTRIBUTES
        if directory is True:
            access |= 0x20  # FILE_TRAVERSE (not FILE_LIST_DIRECTORY)
        elif write:
            access |= 0x00010002  # DELETE | FILE_WRITE_DATA
        elif directory is False:
            access |= 1  # FILE_READ_DATA
        options = 0x00200020  # FILE_OPEN_REPARSE_POINT | FILE_SYNCHRONOUS_IO_NONALERT
        options |= 1 if directory is True else (0x40 if directory is False else 0)

        def perform(sd=None):
            attributes.SecurityDescriptor = sd.value if sd else None
            status = _ntcreate(C.byref(handle), access, C.byref(attributes), C.byref(iosb),
                               None, 0, sharing, 2 if create else 1, options, None, 0)
            if status < 0:
                raise C.WinError(_dos_error(status))
        if create and private:
            with _private_sd() as sd:
                perform(sd)
        else:
            perform()
        try:
            _metadata(handle, directory)
            if private:
                _check_acl(handle)
            return handle.value
        except BaseException:
            _close(handle)
            raise

    def _open_root(anchor, sharing):
        if _drive_type(anchor) != 3:  # DRIVE_FIXED; excludes remote/mapped/removable drives.
            raise ValueError("Only local fixed NTFS volumes are supported")
        handle = _create(anchor, 0x001200A0, sharing, None, 3, 0x02200000, None)
        if handle == W.HANDLE(-1).value:
            raise C.WinError(C.get_last_error())
        try:
            _metadata(handle, True)
            fs, flags = C.create_unicode_buffer(32), W.DWORD()
            _ok(_volume(handle, None, 0, None, None, C.byref(flags), fs, len(fs)))
            if fs.value != "NTFS" or not flags.value & 0x8:  # FILE_PERSISTENT_ACLS
                raise ValueError("NTFS with persistent ACLs is required")
            return handle
        except BaseException:
            _close(handle)
            raise


@contextmanager
def directory_handle(path, *, create=False, private=False, writing=False):
    """Pin every ancestor, return a POSIX fd or native Windows HANDLE.

    Creation never follows existing links. `private` checks the final directory;
    newly created ancestors are private too. The caller must retain this context.
    """
    path = _absolute(path)
    handles = []
    try:
        if WINDOWS:
            anchor, parts = _windows_parts(path)
            # Deny directory mutation on read/SQLite traversal. Publications need
            # FILE_SHARE_WRITE for the kernel's relative rename target open.
            sharing = 3 if writing else 1
            handles.append(_open_root(anchor, sharing))
            for part in parts:
                try:
                    h = _open_child(handles[-1], part, directory=True, sharing=sharing)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        h = _open_child(handles[-1], part, directory=True, create=True,
                                        private=private, sharing=sharing)
                    except FileExistsError:
                        h = _open_child(handles[-1], part, directory=True, sharing=sharing)
                handles.append(h)
            if private:
                _check_acl(handles[-1])
        else:
            handles.append(os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
            for part in path.parts[1:]:
                _component(part)
                if create:
                    try:
                        os.mkdir(part, mode=0o700 if private else 0o755, dir_fd=handles[-1])
                    except FileExistsError:
                        pass
                handles.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                       dir_fd=handles[-1]))
            if private:
                _posix_private(os.fstat(handles[-1]))
        yield handles[-1]
    finally:
        for h in reversed(handles):
            _close(h) if WINDOWS else os.close(h)


def _posix_private(info):
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("State must be private and user-owned")


def _handle_identity(handle):
    if WINDOWS:
        return _identity(handle)
    info = os.fstat(handle)
    return info.st_dev, info.st_ino


def private_directory(path) -> tuple[Path, tuple[int, int]]:
    path = _absolute(path)
    with directory_handle(path, create=True, private=True) as h:
        return path, _handle_identity(h)


def reject_linked_path(path) -> None:
    """Inspect existing components, allowing missing suffixes. Not a write lock.

    Callers doing I/O must use the handle-based operations below, not assume this
    preflight check makes a later pathname open safe against races.
    """
    path = _absolute(path)
    try:
        with directory_handle(path.parent) as parent:
            if WINDOWS:
                h = _open_child(parent, path.name)
                _close(h)
            else:
                info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
                    raise ValueError("Linked path rejected")
    except FileNotFoundError:
        # A missing ancestor can precede a later symlink only lexically, not on disk.
        return
    except OSError as exc:
        raise ValueError("symlink/reparse point or inaccessible path rejected") from exc


def _read_at(parent, name, maximum):
    if WINDOWS:
        h = _open_child(parent, name, directory=False)
        try:
            info = _metadata(h, False)
            if (info.size_high << 32) | info.size_low > maximum:
                raise ValueError("Oversized file")
            data = bytearray()
            while len(data) <= maximum:
                buf = C.create_string_buffer(min(65536, maximum + 1 - len(data)))
                count = W.DWORD()
                _ok(_read(h, buf, len(buf), C.byref(count), None))
                if not count.value:
                    break
                data.extend(buf.raw[:count.value])
            _metadata(h, False)
            return bytes(data)
        finally:
            _close(h)
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
            raise ValueError("Non-regular, hard-linked or oversized file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return stream.read(maximum + 1)
    finally:
        os.close(fd)


def read_file(path, maximum: int) -> bytes:
    path = _absolute(path)
    with directory_handle(path.parent) as parent:
        data = _read_at(parent, path.name, maximum)
    if len(data) > maximum:
        raise ValueError("Oversized file")
    return data


def _publish(path, content, mode, replace):
    path = _absolute(path)
    _component(path.name)
    with directory_handle(path.parent, create=True, writing=True) as parent:
        temporary = ".foundation-" + uuid.uuid4().hex
        if WINDOWS:
            # Windows modes are not ACLs: all files published here are private.
            h = _open_child(parent, temporary, directory=False, create=True, private=True, write=True, sharing=0)
            published = False
            try:
                offset = 0
                while offset < len(content):
                    chunk = content[offset:offset + 65536]
                    buf, count = C.create_string_buffer(chunk), W.DWORD()
                    _ok(_write(h, buf, len(chunk), C.byref(count), None))
                    if not count.value:
                        raise OSError("Zero-length write")
                    offset += count.value
                _ok(_flush(h))
                if replace:
                    try:
                        target = _open_child(parent, path.name, directory=False)
                    except FileNotFoundError:
                        pass
                    else:
                        _close(target)
                encoded = path.name.encode("utf-16-le")
                storage = C.create_string_buffer(C.sizeof(_Rename) + len(encoded))
                rename = _Rename.from_buffer(storage)
                rename.replace, rename.root, rename.length = int(replace), parent, len(encoded)
                C.memmove(C.addressof(storage) + _Rename.name.offset, encoded, len(encoded))
                _ok(_setinfo(h, 3, storage, len(storage)))  # FileRenameInfo
                published = True
            finally:
                try:
                    if not published:
                        delete = W.BOOL(True)
                        _ok(_setinfo(h, 4, C.byref(delete), C.sizeof(delete)))  # FileDispositionInfo
                finally:
                    _close(h)
        else:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         mode, dir_fd=parent)
            try:
                os.fchmod(fd, mode)
                with os.fdopen(fd, "wb", closefd=False) as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(fd)
                if replace:
                    try:
                        info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                            raise ValueError("Linked or non-regular replacement target")
                    os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
                else:
                    os.link(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            finally:
                os.close(fd)
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
            os.fsync(parent)


def write_atomic(path, content: bytes, mode=0o600) -> None:
    """Atomic replacement for installer/state files; rejects existing linked files.

    Windows creates a user-only DACL for every mode (never emulates chmod). Atomic
    visibility is guaranteed on success, not power-loss durability of a rename.
    """
    _publish(path, content, mode, replace=True)


def create_file(path, content: bytes, mode=0o644) -> None:
    """Publish complete bytes create-only; FileExistsError leaves target intact."""
    _publish(path, content, mode, replace=False)


def list_directory(path):
    """Return names while the checked directory and ancestors are retained."""
    with directory_handle(path) as h:
        return os.listdir(path if WINDOWS else h)


def is_directory(path):
    try:
        with directory_handle(path):
            return True
    except (OSError, ValueError):
        return False


@contextmanager
def database_guard(path, identity):
    """Pin private state throughout SQLite use; yield a repeatable sidecar audit."""
    path = _absolute(path)
    with directory_handle(path.parent, private=True) as parent:
        if _handle_identity(parent) != identity:
            raise ValueError("State directory replaced during session")

        def audit():
            for suffix in ("", "-wal", "-shm", "-journal"):
                name = path.name + suffix
                try:
                    if WINDOWS:
                        # SQLite needs write/delete sharing for its own sidecars.
                        h = _open_child(parent, name, directory=False, private=True, sharing=7)
                        _close(h)
                    else:
                        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                        try:
                            info = os.fstat(fd)
                            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                                raise ValueError("Unsafe file type or hardlink")
                            _posix_private(info)
                        finally:
                            os.close(fd)
                except FileNotFoundError:
                    if not suffix:
                        raise ValueError("Missing state database")
                except (OSError, ValueError) as exc:
                    raise ValueError("Unsafe state database or sidecar") from exc
        audit()
        yield audit
