"""외부 통신 및 실제 .env 접근 없이 알림 실패 경로를 검증한다."""
import ast
import asyncio
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch
import urllib.error


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("kia_telegram", ROOT / "telegram.py")
telegram = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(telegram)


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.output))
        self.enterContext(patch.dict(telegram.os.environ, {}, clear=True))
        self.env = self.enterContext(patch.object(telegram, "_load_env", return_value={
            "TELEGRAM_BOT_TOKEN": "test-secret", "TELEGRAM_CHAT_ID": "123",
        }))
        self.urlopen = self.enterContext(patch.object(telegram.urllib.request, "urlopen"))
        self.sleep = self.enterContext(patch.object(telegram.time, "sleep"))

    @staticmethod
    def response(payload):
        return io.BytesIO(json.dumps(payload).encode())

    @staticmethod
    def http_error(code, payload=None):
        return urllib.error.HTTPError(
            "https://example.invalid/test-secret", code, "error", {},
            io.BytesIO(json.dumps(payload or {}).encode()),
        )

    def test_success_and_environment_precedence(self):
        self.urlopen.return_value = self.response({"ok": True})
        with patch.dict(telegram.os.environ, {"TELEGRAM_CHAT_ID": "456"}):
            self.assertTrue(asyncio.run(telegram.send_telegram("test")))
        self.assertIn(b"chat_id=456", self.urlopen.call_args.kwargs["data"])
        self.urlopen.assert_called_once()

    def test_missing_settings_never_connects(self):
        self.env.return_value = {}
        self.assertFalse(telegram._send_sync("test"))
        self.urlopen.assert_not_called()

    def test_transient_http_failure_recovers(self):
        self.urlopen.side_effect = [self.http_error(503), self.response({"ok": True})]
        self.assertTrue(telegram._send_sync("test"))
        self.assertEqual(self.urlopen.call_count, 2)
        self.sleep.assert_called_once_with(1)

    def test_permanent_http_failures_are_not_retried(self):
        for code in (400, 401, 403):
            with self.subTest(code=code):
                self.urlopen.reset_mock()
                self.urlopen.side_effect = self.http_error(code)
                self.assertFalse(telegram._send_sync("test"))
                self.urlopen.assert_called_once()
        self.sleep.assert_not_called()
        self.assertNotIn("test-secret", self.output.getvalue())

    def test_rate_limit_obeys_retry_after(self):
        self.urlopen.side_effect = [
            self.http_error(429, {"parameters": {"retry_after": 4}}),
            self.response({"ok": True}),
        ]
        self.assertTrue(telegram._send_sync("test"))
        self.sleep.assert_called_once_with(4)

    def test_long_rate_limit_does_not_retry_early(self):
        self.urlopen.side_effect = self.http_error(429, {"parameters": {"retry_after": 90}})
        self.assertFalse(telegram._send_sync("test"))
        self.urlopen.assert_called_once()
        self.sleep.assert_not_called()

    def test_timeout_retries_are_bounded(self):
        self.urlopen.side_effect = TimeoutError("test-secret")
        self.assertFalse(telegram._send_sync("test"))
        self.assertEqual(self.urlopen.call_count, 3)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [1, 2])
        self.assertNotIn("test-secret", self.output.getvalue())

    def test_json_api_error_is_not_success(self):
        self.urlopen.return_value = self.response({"ok": False, "error_code": 400})
        self.assertFalse(telegram._send_sync("test"))
        self.urlopen.assert_called_once()

    def test_invalid_response_is_not_success(self):
        self.urlopen.return_value = io.BytesIO(b"not json")
        self.assertFalse(telegram._send_sync("test"))
        self.urlopen.assert_called_once()

    def test_photo_upload_contains_png_caption_and_existing_chat(self):
        self.urlopen.return_value = self.response({"ok": True})
        photo = b'\x89PNG\r\n\x1a\nmock-image'
        self.assertTrue(asyncio.run(telegram.send_telegram_photo(photo, "카카오페이 인증")))
        request = self.urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith('/sendPhoto'))
        self.assertIn('multipart/form-data; boundary=', request.get_header('Content-type'))
        data = self.urlopen.call_args.kwargs['data']
        self.assertIn(b'name="chat_id"\r\n\r\n123\r\n', data)
        self.assertIn('카카오페이 인증'.encode(), data)
        self.assertIn(photo, data)
        self.assertIn(b'filename="kakaopay.png"', data)

    def test_photo_transient_failure_retries_same_image(self):
        self.urlopen.side_effect = [self.http_error(503), self.response({"ok": True})]
        self.assertTrue(asyncio.run(telegram.send_telegram_photo(b'png', 'caption')))
        self.assertEqual(self.urlopen.call_count, 2)
        self.assertEqual(self.urlopen.call_args_list[0].kwargs['data'],
                         self.urlopen.call_args_list[1].kwargs['data'])

    def test_photo_missing_settings_or_empty_image_does_not_connect(self):
        self.assertFalse(asyncio.run(telegram.send_telegram_photo(b'')))
        self.env.return_value = {}
        self.assertFalse(asyncio.run(telegram.send_telegram_photo(b'png')))
        self.urlopen.assert_not_called()


class EnvTests(unittest.TestCase):
    def test_bom_quotes_comments_and_export(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                '# comment\nexport TELEGRAM_BOT_TOKEN="dummy:token" # comment\n'
                "TELEGRAM_CHAT_ID='123'\nPLAIN=value # comment\n"
                'HASH="literal#value"\nBROKEN="unclosed\n', encoding="utf-8-sig")
            with patch.object(telegram, "_ENV_PATH", str(path)), contextlib.redirect_stdout(io.StringIO()):
                values = telegram._load_env()
        self.assertEqual(values["TELEGRAM_BOT_TOKEN"], "dummy:token")
        self.assertEqual(values["TELEGRAM_CHAT_ID"], "123")
        self.assertEqual(values["PLAIN"], "value")
        self.assertEqual(values["HASH"], "literal#value")
        self.assertNotIn("BROKEN", values)


class BookingNotificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # 실제 함수 본문을 실행하되 브라우저/캡차 모듈과 외부 요청은 로드하지 않는다.
        tree = ast.parse((ROOT / "booking.py").read_text(encoding="utf-8"))
        fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_booking")
        self.ns = {
            "time": time, "json": json, "SEAT_MODE": "scan", "SEAT_SESSION_SECONDS": 60,
            "TICKET_COUNT": 1, "MIN_TICKET_COUNT": 1, "PREFER_CONSECUTIVE": True,
            "POLL_INTERVAL_MS": 100, "MAX_POLL_ROUNDS": 1, "FAIL_AFTER_ROUNDS": 1,
            "PREFER_BLOCKS": [], "SEAT_AUTOMATION_JS": "mock",
            "send_telegram": AsyncMock(return_value=True),
            "select_prices_and_continue": AsyncMock(return_value={"ok": True, "reason": "preorder"}),
            "agree_and_continue": AsyncMock(return_value={"ok": True, "reason": "payment"}),
            "click_payment_button": AsyncMock(return_value={"ok": True, "reason": "payment-gateway"}),
            "click_gateway_payment_button": AsyncMock(return_value={"ok": True, "reason": "kakaopay-popup-captured", "screenshot": b"png"}),
            "send_telegram_photo": AsyncMock(return_value=True),
        }
        for name in ("attach_captcha_hook", "_dismiss_notice_popup", "_wait_for_captcha"):
            self.ns[name] = AsyncMock(return_value=None)
        self.ns["wait_for_seat_page"] = AsyncMock(return_value=True)
        self.ns["_extract_context"] = AsyncMock(return_value={})
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "booking_review", "exec"), self.ns)
        self.page = type("FakePage", (), {})()
        self.page.evaluate = AsyncMock()
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    async def test_unrelated_error_requires_review_without_notification(self):
        self.page.evaluate.side_effect = RuntimeError("fetch failed")
        result = await self.ns["run_booking"](self.page)
        self.assertFalse(result["ok"])
        self.assertTrue(result["requires_review"])
        self.ns["send_telegram"].assert_not_awaited()

    async def test_none_is_unknown_not_success(self):
        self.page.evaluate.return_value = None
        result = await self.ns["run_booking"](self.page)
        self.assertFalse(result["ok"])
        self.assertTrue(result["requires_review"])
        self.ns["send_telegram"].assert_not_awaited()

    async def test_alert_failure_preserves_reservation_and_records_failure(self):
        self.ns["send_telegram"].return_value = False
        self.page.evaluate.return_value = {"ok": True, "seats": [{
            "rowNo": "1", "seatNo": "2", "seatGrade": "A", "price": 1, "seatInfoId": "dummy",
        }]}
        result = await self.ns["run_booking"](self.page)
        self.assertTrue(result["ok"])
        self.assertFalse(result["notification_sent"])
        self.page.evaluate.assert_awaited_once()
        self.ns["send_telegram"].assert_awaited_once()
        self.assertIn("좌석 선점 성공", self.ns["send_telegram"].call_args.args[0])

    async def test_known_failure_does_not_send_success_alert_or_require_review(self):
        self.page.evaluate.return_value = {"ok": False, "reason": "timeout"}
        result = await self.ns["run_booking"](self.page)
        self.assertFalse(result["ok"])
        self.assertFalse(result.get("requires_review", False))
        self.ns["send_telegram"].assert_not_awaited()

    async def test_price_failure_preserves_seats_and_requests_review(self):
        self.ns["TICKET_COUNT"] = 4
        seats = [{"rowNo": "1", "seatNo": str(n), "seatGrade": "A", "price": 1,
                  "seatInfoId": f"dummy-{n}"} for n in range(2)]
        self.page.evaluate.return_value = {"ok": True, "seats": seats}
        self.ns["select_prices_and_continue"].return_value = {
            "ok": False, "reason": "wait-preorder", "requires_review": True,
        }
        result = await self.ns["run_booking"](self.page)
        self.assertTrue(result["ok"])
        self.assertTrue(result["requires_review"])
        self.assertEqual(result["seats"], seats)
        self.ns["select_prices_and_continue"].assert_awaited_once_with(self.page, 2)
        self.ns["agree_and_continue"].assert_not_awaited()
        self.ns["click_payment_button"].assert_not_awaited()
        self.page.evaluate.assert_awaited_once()
        self.assertIn("브라우저에서 확인", self.ns["send_telegram"].call_args.args[0])

    async def test_preorder_failure_preserves_seats_and_requests_review(self):
        self.page.evaluate.return_value = {"ok": True, "seats": [{
            "rowNo": "1", "seatNo": "2", "seatGrade": "A", "price": 1, "seatInfoId": "dummy",
        }]}
        self.ns["agree_and_continue"].return_value = {
            "ok": False, "reason": "wait-payment", "requires_review": True,
        }
        result = await self.ns["run_booking"](self.page)
        self.assertTrue(result["ok"])
        self.assertTrue(result["requires_review"])
        self.ns["agree_and_continue"].assert_awaited_once_with(self.page)
        self.page.evaluate.assert_awaited_once()
        self.assertIn("예매 정보와 안내", self.ns["send_telegram"].call_args.args[0])
        self.ns["click_payment_button"].assert_not_awaited()

    async def test_success_continues_to_payment_and_notifies(self):
        self.page.evaluate.return_value = {"ok": True, "seats": [{
            "rowNo": "1", "seatNo": "2", "seatGrade": "A", "price": 1, "seatInfoId": "dummy",
        }]}
        result = await self.ns["run_booking"](self.page)
        self.assertTrue(result["preorder"]["ok"])
        self.assertFalse(result.get("requires_review", False))
        self.ns["agree_and_continue"].assert_awaited_once_with(self.page)
        self.ns["click_payment_button"].assert_awaited_once_with(self.page)
        self.assertTrue(result["payment_entry"]["ok"])
        self.ns["click_gateway_payment_button"].assert_awaited_once_with(self.page)
        self.assertTrue(result["payment_submit"]["ok"])
        self.assertTrue(result["payment_screenshot_sent"])
        self.assertNotIn("screenshot", result["payment_submit"])
        self.ns["send_telegram_photo"].assert_awaited_once()
        self.assertEqual(self.ns["send_telegram_photo"].call_args.args[0], b"png")
        self.assertIn("새 탭 화면을 텔레그램으로 보냈습니다", self.ns["send_telegram"].call_args.args[0])

    async def test_payment_click_failure_preserves_seats_and_requests_review(self):
        self.page.evaluate.return_value = {"ok": True, "seats": [{
            "rowNo": "1", "seatNo": "2", "seatGrade": "A", "price": 1, "seatInfoId": "dummy",
        }]}
        self.ns["click_payment_button"].return_value = {
            "ok": False, "reason": "click-payment-button", "requires_review": True,
        }
        result = await self.ns["run_booking"](self.page)
        self.assertTrue(result["ok"])
        self.assertTrue(result["requires_review"])
        self.ns["click_payment_button"].assert_awaited_once_with(self.page)
        self.ns["click_gateway_payment_button"].assert_not_awaited()
        self.page.evaluate.assert_awaited_once()
        self.assertIn("결제 상태를 확인", self.ns["send_telegram"].call_args.args[0])

    async def test_gateway_click_failure_preserves_seats_and_requests_review(self):
        self.page.evaluate.return_value = {"ok": True, "seats": [{
            "rowNo": "1", "seatNo": "2", "seatGrade": "A", "price": 1, "seatInfoId": "dummy",
        }]}
        self.ns["click_gateway_payment_button"].return_value = {
            "ok": False, "reason": "click-gateway-payment-button", "requires_review": True,
        }
        result = await self.ns["run_booking"](self.page)
        self.assertTrue(result["ok"])
        self.assertTrue(result["requires_review"])
        self.ns["click_gateway_payment_button"].assert_awaited_once_with(self.page)
        self.ns["send_telegram_photo"].assert_not_awaited()
        self.page.evaluate.assert_awaited_once()
        self.assertIn("결제 상태를 확인", self.ns["send_telegram"].call_args.args[0])

    async def test_photo_failure_preserves_seats_without_clicking_again(self):
        self.page.evaluate.return_value = {"ok": True, "seats": [{
            "rowNo": "1", "seatNo": "2", "seatGrade": "A", "price": 1, "seatInfoId": "dummy",
        }]}
        self.ns["send_telegram_photo"].return_value = False
        result = await self.ns["run_booking"](self.page)
        self.assertTrue(result["ok"])
        self.assertTrue(result["requires_review"])
        self.assertFalse(result["payment_screenshot_sent"])
        self.ns["click_gateway_payment_button"].assert_awaited_once_with(self.page)
        self.page.evaluate.assert_awaited_once()
        self.assertIn("사진 전송에 실패", self.ns["send_telegram"].call_args.args[0])


if __name__ == "__main__":
    unittest.main()
