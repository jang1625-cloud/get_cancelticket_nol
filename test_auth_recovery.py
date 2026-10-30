"""외부 API/예매 없이 인증 복구 대상 선택을 검증한다."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import line_up


def game(goods_code="26014439", place_code="26000116", date="2026-11-01", time="14:00"):
    return {
        "goodsCode": goods_code,
        "placeCode": place_code,
        "sport": {"playDate": date, "playTime": time},
    }


class RecoveryDataTests(unittest.TestCase):
    def test_initial_data_after_text_record(self):
        target = game()
        payload = {"state": {"data": {"content": [target]}}}
        flight = '28:T3,abc29:' + json.dumps(["$", "component", None, payload]) + '\n'
        self.assertEqual(list(line_up._sports_games_from_flight(flight)), [target])

    def test_malformed_record_does_not_hide_valid_record(self):
        flight = '1:{broken\n2:' + json.dumps({"content": [game()]})
        self.assertEqual(list(line_up._sports_games_from_flight(flight)), [game()])

    def test_split_initial_data_scripts(self):
        flight = '1:' + json.dumps({"content": [game()]})
        scripts = ["self.__next_f.push(" + json.dumps([1, part]) + ")"
                   for part in (flight[:35], flight[35:])]
        self.assertEqual(list(line_up._sports_games_from_scripts(scripts)), [game()])

    def test_schedule_comes_from_selected_game(self):
        pattern = line_up._recovery_date_pattern(game())
        self.assertIsNotNone(pattern.fullmatch("11월 1일(일) 14:00"))
        self.assertIsNotNone(pattern.fullmatch("11월 1일 14:00"))
        self.assertIsNone(pattern.fullmatch("10월 20일(화) 18:30"))
        self.assertIsNone(pattern.fullmatch("11월 1일(일) 18:30"))

    def test_missing_schedule_stops_recovery(self):
        with self.assertRaises(RuntimeError):
            line_up._recovery_date_pattern({"sport": {}})


class RecoverySelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrong_place_is_not_selected(self):
        page = SimpleNamespace(
            url=line_up.RECOVERY_LIST_URL,
            on=lambda *args: None,
            remove_listener=lambda *args: None,
            locator=lambda *args: SimpleNamespace(all_text_contents=AsyncMock(return_value=[
                "self.__next_f.push(" + json.dumps([1, '1:' + json.dumps({"content": [game(place_code="wrong") ]})]) + ")"
            ])),
            get_by_text=AsyncMock(),
            get_by_role=lambda *args, **kwargs: SimpleNamespace(count=AsyncMock(return_value=0)),
        )
        with patch.object(line_up, "time", SimpleNamespace(monotonic=iter([0, 16]).__next__)):
            with self.assertRaisesRegex(RuntimeError, "goodsCode=26014439, placeCode=26000116"):
                await line_up.find_auth_recovery_button(page, "26014439", "26000116")
        page.get_by_text.assert_not_called()

    async def test_401_forwards_both_codes(self):
        class StopAfterNotification(Exception):
            pass

        page = SimpleNamespace(request=object())
        recovery = AsyncMock(side_effect=RuntimeError("test: unavailable"))
        with (
            patch.object(line_up, "fetch_member_info", AsyncMock(side_effect=line_up.LineUpError("member-info", 401))),
            patch.object(line_up, "click_product_for_auth_recovery", recovery),
            patch.object(line_up, "send_telegram", AsyncMock()),
            patch.object(line_up, "wait_for_enter", AsyncMock(side_effect=StopAfterNotification)),
        ):
            with self.assertRaises(StopAfterNotification):
                await line_up.run_line_up(page, goods_code="custom-goods", place_code="custom-place")
        recovery.assert_awaited_once_with(page, "custom-goods", "custom-place")


if __name__ == "__main__":
    unittest.main()
