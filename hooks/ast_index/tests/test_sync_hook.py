#!/usr/bin/env python3
"""Unit tests for hooks/ast_index/sync_hook.py.
Stdlib unittest only.

Run: python3 -m unittest discover -s hooks/ast_index/tests
"""
import importlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOOK_DIR))
sync_hook = importlib.import_module("sync_hook")


class TestIsTrackedSource(unittest.TestCase):
    def test_py_file_matches(self):
        self.assertTrue(sync_hook.is_tracked_source(os.path.join("scripts", "ast_index.py")))

    def test_non_py_file_does_not_match(self):
        self.assertFalse(sync_hook.is_tracked_source("AGENTS.md"))

    def test_venv_file_does_not_match(self):
        self.assertFalse(sync_hook.is_tracked_source(os.path.join(".venv", "lib", "x.py")))

    def test_pycache_file_does_not_match(self):
        self.assertFalse(sync_hook.is_tracked_source(os.path.join("scripts", "__pycache__", "x.py")))

    def test_node_modules_file_does_not_match(self):
        self.assertFalse(sync_hook.is_tracked_source(os.path.join("node_modules", "x", "y.py")))


class TestMainEndToEnd(unittest.TestCase):
    """Patches AST_INDEX_SCRIPT to a fake script - never invokes the real scripts/ast_index.py.
    These tests never touch this repo's own agent_docs/ast_index/.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._old_script = sync_hook.AST_INDEX_SCRIPT
        self._old_cwd = os.getcwd()
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self._old_cwd)
        sync_hook.AST_INDEX_SCRIPT = self._old_script
        self._tmp.cleanup()

    def write_fake_script(self, exit_code: int, stderr: str = "") -> Path:
        script = self.root / "fake_build.py"
        script.write_text(
            "import sys\n"
            f"sys.stderr.write({stderr!r})\n"
            f"sys.exit({exit_code})\n",
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        sync_hook.AST_INDEX_SCRIPT = script
        return script

    def run_hook(self, file_path: str):
        # sync_hook.main() forwards the build script's stderr on failure - intended for the live hook.
        # That's noise a passing test shouldn't print.
        payload = json.dumps({"tool_input": {"file_path": file_path}})
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(payload)
        try:
            with redirect_stderr(io.StringIO()):
                return sync_hook.main()
        finally:
            sys.stdin = old_stdin

    def test_non_tracked_source_exits_zero_without_invoking_script(self):
        self.write_fake_script(exit_code=1)  # would fail if invoked
        code = self.run_hook(str(self.root / "README.md"))
        self.assertEqual(code, 0)

    def test_py_edit_invokes_script_success(self):
        self.write_fake_script(exit_code=0)
        src = self.root / "scripts" / "thing.py"
        code = self.run_hook(str(src))
        self.assertEqual(code, 0)

    def test_py_edit_invokes_script_failure(self):
        self.write_fake_script(exit_code=1, stderr="boom")
        src = self.root / "scripts" / "thing.py"
        code = self.run_hook(str(src))
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
