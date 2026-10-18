"""실제 Telegram 전송이나 터미널 입력 없이 반복 알림과 취소를 검증한다."""

import asyncio
import contextlib
import io
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import payment_reminder as reminders


class ReminderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.output = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(patch.dict(reminders.os.environ, {}, clear=True))
        self.env = self.enterContext(patch.object(reminders, "_load_env", return_value={
            "TELEGRAM_BOT_TOKEN": "test-secret", "TELEGRAM_CHAT_ID": "123",
        }))
        self.client = self.enterContext(patch.object(reminders.aiohttp, "ClientSession"))
        self.session = self.client.return_value.__aenter__.return_value
        self.session.post = MagicMock(side_effect=self.response)
        self.sleep = self.enterContext(patch.object(reminders.asyncio, "sleep", new_callable=AsyncMock))

    @staticmethod
    def response(*args, status=200, payload=None, **kwargs):
        request = MagicMock()
        response = request.__aenter__.return_value
        response.status = status
        response.json = AsyncMock(return_value=payload if payload is not None else {"ok": True})
        return request

    async def test_400_messages_and_default_interval(self):
        with patch.dict(reminders.os.environ, {"TELEGRAM_CHAT_ID": "456"}):
            await reminders.send_payment_reminders()
        self.assertEqual(self.session.post.call_count, 400)
        self.assertEqual(self.sleep.await_count, 400)
        self.assertEqual(self.sleep.call_args_list[0].args, (1.5,))
        self.assertTrue(all(0 <= call.args[0] <= 1.5 for call in self.sleep.call_args_list))
        messages = [call.kwargs["data"] for call in self.session.post.call_args_list]
        self.assertTrue(all(message["chat_id"] == "456" for message in messages))
        self.assertIn("(1/400)", messages[0]["text"])
        self.assertIn("(400/400)", messages[-1]["text"])
        self.assertIn("빨리 확인해주세요", messages[-1]["text"])
        self.client.return_value.__aexit__.assert_awaited_once()

    async def test_missing_credentials_never_connects(self):
        self.env.return_value = {}
        await reminders.send_payment_reminders()
        self.client.assert_not_called()

    async def test_rate_limit_waits_without_retrying_same_message(self):
        self.session.post.side_effect = [
            self.response(status=429, payload={"ok": False, "parameters": {"retry_after": 90}}),
            self.response(),
        ]
        with patch.object(reminders, "REMINDER_COUNT", 2):
            await reminders.send_payment_reminders()
        self.assertEqual(self.sleep.call_args_list[1].args, (90,))
        self.assertEqual(self.session.post.call_count, 2)
        self.assertIn("(2/2)", self.session.post.call_args.kwargs["data"]["text"])

    async def test_permanent_error_stops_early(self):
        for code in (400, 401, 403):
            with self.subTest(code=code):
                self.session.post.reset_mock()
                self.session.post.side_effect = lambda *a, **kw: self.response(status=code, payload={"ok": False})
                await reminders.send_payment_reminders()
                self.session.post.assert_called_once()

    async def test_rate_limit_without_valid_delay_stops(self):
        for value in (None, -1, float("inf"), "90", True):
            with self.subTest(value=value):
                self.session.post.reset_mock()
                self.session.post.side_effect = lambda *a, **kw: self.response(
                    status=429, payload={"parameters": {"retry_after": value}})
                await reminders.send_payment_reminders()
                self.session.post.assert_called_once()

    async def test_network_failures_are_bounded_and_secrets_are_not_logged(self):
        self.session.post.side_effect = TimeoutError("test-secret")
        with patch.object(reminders, "REMINDER_COUNT", 3):
            await reminders.send_payment_reminders()
        self.assertEqual(self.session.post.call_count, 3)
        self.assertNotIn("test-secret", self.output.getvalue())

    async def test_enter_cancels_inflight_request_and_closes_session(self):
        entered_request = asyncio.Event()
        cancelled = asyncio.Event()

        async def blocked_json(**kwargs):
            entered_request.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        response = self.response()
        response.__aenter__.return_value.json.side_effect = blocked_json
        self.session.post.side_effect = None
        self.session.post.return_value = response

        async def enter(prompt):
            await entered_request.wait()

        await asyncio.wait_for(reminders.wait_for_payment_reminders(enter), timeout=1)
        self.assertTrue(cancelled.is_set())
        self.session.post.assert_called_once()
        response.__aexit__.assert_awaited_once()
        self.client.return_value.__aexit__.assert_awaited_once()

    async def test_enter_interrupts_interval_before_first_message(self):
        sleeping = asyncio.Event()

        async def interval(delay):
            sleeping.set()
            await asyncio.Event().wait()

        self.sleep.side_effect = interval

        async def enter(prompt):
            await sleeping.wait()

        await asyncio.wait_for(reminders.wait_for_payment_reminders(enter), timeout=1)
        self.session.post.assert_not_called()

    async def test_enter_interrupts_rate_limit_wait(self):
        rate_limited = asyncio.Event()

        async def interval(delay):
            if delay >= 90:
                rate_limited.set()
                await asyncio.Event().wait()

        self.sleep.side_effect = interval
        self.session.post.side_effect = lambda *a, **kw: self.response(
            status=429, payload={"parameters": {"retry_after": 90}})

        async def enter(prompt):
            await rate_limited.wait()

        await asyncio.wait_for(reminders.wait_for_payment_reminders(enter), timeout=1)
        self.session.post.assert_called_once()

    async def test_after_limit_keeps_one_enter_waiter_until_user_confirms(self):
        completed = asyncio.Event()
        pressed = asyncio.Event()

        async def finished():
            completed.set()

        async def enter(prompt):
            await pressed.wait()

        waiter = AsyncMock(side_effect=enter)
        with patch.object(reminders, "send_payment_reminders", side_effect=finished):
            task = asyncio.create_task(reminders.wait_for_payment_reminders(waiter))
            await asyncio.wait_for(completed.wait(), timeout=1)
            self.assertFalse(task.done())
            pressed.set()
            await asyncio.wait_for(task, timeout=1)
        waiter.assert_awaited_once()

    async def test_input_eof_cleans_up_reminder(self):
        with self.assertRaises(EOFError):
            await reminders.wait_for_payment_reminders(AsyncMock(side_effect=EOFError))
        self.session.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
