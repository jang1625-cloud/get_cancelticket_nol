"""Offline checks for terminal logging and member-info authentication alerts."""

import ast
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock

from terminal_log import terminal_log


ROOT = Path(__file__).resolve().parents[1]


class TerminalLogTests(unittest.TestCase):
    def test_legacy_console_keeps_unicode_in_log(self):
        with tempfile.TemporaryDirectory() as directory:
            with io.TextIOWrapper(io.BytesIO(), encoding="cp949") as console:
                with contextlib.redirect_stdout(console):
                    with terminal_log("legacy", directory) as path:
                        print("경고 ⚠️")
                self.assertIn("경고 ⚠️", path.read_text(encoding="utf-8"))

    def test_stdout_stderr_and_unique_files(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                with terminal_log("test", directory) as first:
                    print("한글 로그 ⚠️")
                    print("error output", file=sys.stderr)
                    self.assertIn("error output", first.read_text(encoding="utf-8"))
                self.assertIs(sys.stdout, stdout)
                self.assertIs(sys.stderr, stderr)
                with terminal_log("test", directory) as second:
                    print("second run")
            self.assertNotEqual(first, second)
            saved = first.read_text(encoding="utf-8")
            self.assertIn("한글 로그 ⚠️", saved)
            self.assertIn("error output", saved)
            self.assertNotIn("second run", saved)
        self.assertIn("한글 로그 ⚠️", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "error output\n")

    def test_exception_is_saved_and_streams_restored(self):
        stdout, stderr = sys.stdout, sys.stderr
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "test failure"):
                with terminal_log("failure", directory) as path:
                    raise RuntimeError("test failure")
            self.assertIn("RuntimeError: test failure", path.read_text(encoding="utf-8"))
        self.assertIs(sys.stdout, stdout)
        self.assertIs(sys.stderr, stderr)


class LoginRequired(Exception):
    pass


class MemberInfoAlertTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Run the real handler without importing browser/captcha dependencies.
        tree = ast.parse((ROOT / "line_up.py").read_text(encoding="utf-8"))
        nodes = [node for node in tree.body
                 if isinstance(node, (ast.ClassDef, ast.AsyncFunctionDef))
                 and node.name in {"LineUpError", "run_line_up"}]
        self.ns = dict.fromkeys(
            ("GOODS_CODE", "BIZ_CODE", "PLACE_CODE", "CHANNEL_CODE", "LANG", "FROM"), "test")
        self.ns.update(send_telegram=AsyncMock(return_value=True),
                       wait_for_enter=AsyncMock(side_effect=LoginRequired))
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "line_up.py", "exec"), self.ns)
        self.page = type("Page", (), {"request": object()})()
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    async def check_status(self, status):
        self.ns["fetch_member_info"] = AsyncMock(
            side_effect=self.ns["LineUpError"]("member-info", status, "response body"))
        with self.assertRaises(LoginRequired):
            await self.ns["run_line_up"](self.page)
        self.ns["wait_for_enter"].assert_awaited_once()

    async def test_401_alerts_before_waiting_for_login(self):
        async def wait(message):
            self.ns["send_telegram"].assert_awaited_once()
            raise LoginRequired

        self.ns["wait_for_enter"].side_effect = wait
        await self.check_status(401)
        message = self.ns["send_telegram"].call_args.args[0]
        self.assertIn("member-info 실패(status=401 Unauthorized)", message)
        self.assertNotIn("response body", message)

    async def test_other_status_does_not_send_authentication_alert(self):
        await self.check_status(500)
        self.ns["send_telegram"].assert_not_awaited()

    async def test_failed_notification_still_waits_for_login(self):
        self.ns["send_telegram"].return_value = False
        await self.check_status(401)
        self.ns["send_telegram"].assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
