"""Check how the host smoke test recognizes configured writable mounts."""

import importlib.util
import os
from pathlib import Path
import subprocess
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("host_smoke", Path(__file__).with_name("host-smoke-test.py"))
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)

GIT = "/workspace/Git"
PROJECT = GIT + "/project"


class WritableMountTest(unittest.TestCase):
    def mounts(self, args, directories):
        command = ["python3", "-I", "acp-broker.py", "--profile", "codex", "--git-root", GIT, *args]
        cmdline = b"\0".join(os.fsencode(arg) for arg in command) + b"\0"
        opened = mock.mock_open(read_data=cmdline)
        with mock.patch.object(smoke, "GIT", GIT), \
             mock.patch.object(smoke, "sh", return_value=subprocess.CompletedProcess([], 0, "123\n")), \
             mock.patch("builtins.open", opened), \
             mock.patch.object(smoke.os.path, "isdir", side_effect=lambda path: path in directories):
            result = smoke.broker_writable_mounts("codex", PROJECT)
        opened.assert_called_once_with("/proc/123/cmdline", "rb")
        return result

    def test_shared_writable_folders_are_expected_mounts(self):
        folders = [GIT + "/shared", GIT + "/notes with spaces"]
        result = self.mounts(["--memory", "/notes", "--writable", folders[0],
                              "--writable=" + folders[1]], folders)
        self.assertEqual(result, {path + ":true" for path in folders})

    def test_folders_inside_project_do_not_need_separate_mounts(self):
        folders = [PROJECT, PROJECT + "/nested", PROJECT + "-other"]
        result = self.mounts([arg for path in folders for arg in ("--writable", path)], folders)
        self.assertEqual(result, {PROJECT + "-other:true"})

    def test_missing_and_outside_folders_are_not_allowed(self):
        result = self.mounts(["--writable", GIT + "/missing", "--writable", GIT,
                              "--writable", "/outside"], [GIT, "/outside"])
        self.assertEqual(result, set())

    def test_no_shared_folders_does_not_allow_any_extra_mounts(self):
        self.assertEqual(self.mounts([], []), set())

    def test_stopped_or_unreadable_broker_fails(self):
        for result in (subprocess.CompletedProcess([], 0, "0\n"),
                       subprocess.CompletedProcess([], 1, "")):
            with self.subTest(result=result), mock.patch.object(smoke, "sh", return_value=result):
                with self.assertRaises(RuntimeError):
                    smoke.broker_writable_mounts("claude", PROJECT)
        with mock.patch.object(smoke, "sh", return_value=subprocess.CompletedProcess([], 0, "123\n")), \
             mock.patch("builtins.open", side_effect=PermissionError("not readable")):
            with self.assertRaises(PermissionError):
                smoke.broker_writable_mounts("claude", PROJECT)


if __name__ == "__main__":
    unittest.main()
