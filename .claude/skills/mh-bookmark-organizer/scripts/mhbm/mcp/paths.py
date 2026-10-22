"""Path confinement for the tools that read or write a file the caller named.

Rules (all enforced here, none left to the command line):

* ``~`` is expanded and relative names are resolved against a fixed base *before* the check, and the **resolved absolute path is what
  the tool uses** (the old code checked the expanded path but handed the raw string to the CLI, so the two could differ);
* symlinks are resolved (``realpath``) and the result must lie inside an allowed root and outside every denied root (the library);
* the file is then *opened here* with ``O_NOFOLLOW`` and the descriptor is verified against its real location (``/proc/self/fd`` or
  ``F_GETPATH``), which closes the check-then-open race: whatever the path pointed to a moment ago, the descriptor we hold either is
  inside the allowed root or is refused;
* reads accept regular files only (no FIFOs, devices, directories) and are size-capped; writes create a *new* file (``O_EXCL``), never
  overwrite, and never land inside the library directory.
"""
from __future__ import annotations

import os
import stat
import sys
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

MAX_PATH = 4096


class PathError(ValueError):
    """A path the caller supplied is not acceptable; the message is safe to show to the model."""


def fd_path(fd: int):
    """The real location of an open descriptor, or None when the platform cannot tell."""
    try:
        if sys.platform.startswith("linux"):
            return os.readlink(f"/proc/self/fd/{fd}")
        if sys.platform == "darwin":                             # pragma: no cover - macOS only
            import fcntl
            return fcntl.fcntl(fd, 50, b"\0" * 1024).split(b"\0", 1)[0].decode()   # F_GETPATH
        if sys.platform == "win32":                              # pragma: no cover - Windows only
            import ctypes
            import msvcrt

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            get_path = kernel32.GetFinalPathNameByHandleW
            get_path.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32)
            get_path.restype = ctypes.c_uint32
            buf = ctypes.create_unicode_buffer(32768)
            size = get_path(msvcrt.get_osfhandle(fd), buf, len(buf), 0)
            if not size or size >= len(buf):
                return None
            path = buf.value
            if path.startswith("\\\\?\\UNC\\"):
                return "\\\\" + path[8:]
            return path[4:] if path.startswith("\\\\?\\") else path
    except (OSError, ValueError):
        return None
    return None


def _norm(path: str) -> str:
    return os.path.normcase(path)


def _within(path: str, root: str) -> bool:
    path, root = _norm(path), _norm(root)
    if path == root:
        return True
    prefix = root if root.endswith(os.sep) else root + os.sep
    return path.startswith(prefix)


def uri_to_path(uri: str):
    """``file:///home/me/x`` -> ``/home/me/x`` (None for any other scheme)."""
    parts = urlparse(uri)
    if parts.scheme != "file":
        return None
    path = url2pathname(unquote(parts.path))
    if parts.netloc and parts.netloc != "localhost":             # file://server/share -> UNC
        path = "//" + parts.netloc + path
    return path or None


class PathPolicy:
    def __init__(self, allow_dirs, deny_dirs=(), base=None):
        self.base = os.path.realpath(base or os.getcwd())
        self.deny = [os.path.realpath(d) for d in deny_dirs]
        self.allow: list = []
        self.set_allow(allow_dirs)

    def set_allow(self, dirs) -> None:
        self.allow = [os.path.realpath(os.path.expanduser(d)) for d in dirs if d]

    def describe(self) -> str:
        return ", ".join(self.allow) or "(none)"

    def inside(self, real: str) -> bool:
        return not any(_within(real, d) for d in self.deny) and any(_within(real, a) for a in self.allow)

    def resolve(self, raw) -> str:
        if not isinstance(raw, str) or not raw.strip():
            raise PathError("a file path is required")
        if len(raw) > MAX_PATH or any(ord(c) < 32 for c in raw):
            raise PathError("the path is too long or contains control characters")
        try:
            real = os.path.realpath(os.path.join(self.base, os.path.expanduser(raw)))
        except (OSError, ValueError, RuntimeError) as e:
            raise PathError("the path cannot be resolved") from e
        if not self.inside(real):
            raise PathError(f"path is outside the allowed directories ({self.describe()}); the server's operator can widen this with --allow-dir")
        return real

    def _verify(self, fd: int, what: str) -> None:
        actual = fd_path(fd)
        if actual is None:
            raise PathError(f"cannot verify the location of {what} on this platform")
        if not self.inside(os.path.realpath(actual.replace(" (deleted)", ""))):
            raise PathError(f"{what} resolves outside the allowed directories")

    # ------------------------------------------------------------------ read
    def open_for_read(self, raw, max_bytes: int):
        """Return ``(fd, size)`` for a regular file inside the allowed roots; the caller closes ``fd``."""
        real = self.resolve(raw)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(real, flags)
        except OSError as e:
            raise PathError(f"cannot open the file ({e.strerror or 'error'})") from e
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise PathError("not a regular file")
            if st.st_size > max_bytes:
                raise PathError(f"the file is larger than the {max_bytes // (1 << 20)} MiB limit")
            self._verify(fd, "the file")
        except BaseException:
            os.close(fd)
            raise
        return fd, st.st_size

    # ----------------------------------------------------------------- write
    def create_new(self, raw):
        """Create (never overwrite) a file inside the allowed roots; return ``(fd, real_path)``."""
        real = self.resolve(raw)
        if not os.path.isdir(os.path.dirname(real)):
            raise PathError("the directory does not exist")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(real, flags, 0o666)
        except FileExistsError as e:
            raise PathError("a file with that name already exists; the tool never overwrites, pick a new name") from e
        except OSError as e:
            raise PathError(f"cannot create the file ({e.strerror or 'error'})") from e
        try:
            self._verify(fd, "the new file")
        except PathError:
            os.close(fd)
            try:
                os.unlink(real)                                   # we created it (O_EXCL), so removing it is safe
            except OSError:
                pass
            raise
        return fd, real
