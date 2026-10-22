import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import common  # noqa: F401
from mhbm.mcp import paths, registry


class PathPolicy(unittest.TestCase):
    def test_import_copy_enforces_a_limit_without_buffering_the_whole_file(self):
        destination = io.BytesIO()
        limit = 1 << 20
        with self.assertRaisesRegex(paths.PathError, "larger than the 1 MiB limit"):
            registry._copy_limited(io.BytesIO(b"x" * (limit + 1)), destination, limit)
        self.assertEqual(len(destination.getvalue()), limit)

    def test_unverifiable_open_handle_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d, "input.html")
            target.write_text("<DL></DL>", encoding="utf-8")
            policy = paths.PathPolicy([d])
            with mock.patch.object(paths, "fd_path", return_value=None):
                with self.assertRaisesRegex(paths.PathError, "cannot verify the location of the file"):
                    policy.open_for_read(str(target), 1024)

    @unittest.skipUnless(sys.platform == "win32", "Windows handle APIs")
    def test_windows_final_path_prefixes_are_normalized(self):
        with mock.patch.object(paths.sys, "platform", "win32"), \
             mock.patch("msvcrt.get_osfhandle", return_value=42), \
             mock.patch("ctypes.WinDLL") as win_dll:
            def get_final_path(handle, buffer, length, flags):
                buffer.value = r"\\?\C:\Users\test\Downloads\input.html"
                return len(buffer.value)

            win_dll.return_value.GetFinalPathNameByHandleW.side_effect = get_final_path
            self.assertEqual(paths.fd_path(7), r"C:\Users\test\Downloads\input.html")

    @unittest.skipUnless(sys.platform == "win32", "Windows handle APIs")
    def test_windows_open_handle_is_verified_against_allowed_root(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d, "input.html")
            target.write_text("<DL></DL>", encoding="utf-8")
            policy = paths.PathPolicy([d])
            fd, _size = policy.open_for_read(str(target), 1024)
            import os
            os.close(fd)
