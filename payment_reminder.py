"""카카오페이 화면 확인 알림. Enter 입력 시 대기와 진행 중인 HTTP 요청을 취소한다."""

import asyncio
import contextlib
import math
import os

import aiohttp

from telegram import _load_env


REMINDER_INTERVAL_SECONDS = 1.5
REMINDER_COUNT = 40


async def send_payment_reminders():
    """요청을 겹치거나 재시도하지 않고 최대 400회 전송한다."""
    env = _load_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or env.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or env.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("⚠️ 텔레그램 설정 없음 — 반복 알림 생략")
        return

    loop = asyncio.get_running_loop()
    delay = REMINDER_INTERVAL_SECONDS
    sent = 0
    # 기존 동기 전송의 작업 스레드는 취소해도 HTTP 재시도가 남을 수 있다.
    # 반복 알림은 취소 가능한 비동기 요청으로 보내며 자동 재시도하지 않는다.
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        for number in range(1, REMINDER_COUNT + 1):
            await asyncio.sleep(delay)
            started = loop.time()
            retry_after = 0
            try:
                async with session.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    data={"chat_id": chat_id, "text": (
                        f"🚨 빨리 확인해주세요! 카카오페이 결제 화면이 열렸습니다. "
                        f"인증과 결제 결과를 확인하세요. ({number}/{REMINDER_COUNT})"
                    )},
                ) as response:
                    payload = await response.json(content_type=None)
                    if response.status == 200 and isinstance(payload, dict) and payload.get("ok") is True:
                        sent += 1
                        print(f"📨 결제 확인 알림 {number}/{REMINDER_COUNT}")
                    else:
                        code = payload.get("error_code", response.status) if isinstance(payload, dict) else response.status
                        print(f"⚠️ 결제 확인 알림 실패 ({number}/{REMINDER_COUNT})")
                        if code == 429:
                            params = payload.get("parameters", {}) if isinstance(payload, dict) else {}
                            value = params.get("retry_after") if isinstance(params, dict) else None
                            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                                    or not math.isfinite(value) or value < 0):
                                print("⚠️ Telegram 전송 제한의 대기 시간을 확인할 수 없어 반복 알림 중단")
                                return
                            retry_after = value
                        elif isinstance(code, int) and 400 <= code < 500:
                            print("⚠️ Telegram 설정 또는 채팅 권한 오류 — 반복 알림 중단")
                            return
            except Exception as exc:
                # URL/응답/예외 메시지에는 봇 토큰이 들어갈 수 있다.
                print(f"⚠️ 결제 확인 알림 통신 실패: {type(exc).__name__}")
            # 정상 응답은 시작 시각 기준 1.5초 간격. 느린 요청은 겹치지 않으며
            # Telegram의 전송 제한 시에는 응답 시점부터 지정 시간을 기다린다.
            delay = max(0, REMINDER_INTERVAL_SECONDS - (loop.time() - started), retry_after)
    print(f"🔕 반복 알림 종료: {REMINDER_COUNT}회 시도, {sent}회 전송 성공")


async def wait_for_payment_reminders(wait_for_enter):
    """400회가 끝나도 브라우저를 유지하고 동일한 Enter 입력을 계속 기다린다."""
    print("🔔 결제 확인 알림을 1500ms 간격으로 최대 400회 보냅니다.")
    reminder = asyncio.create_task(send_payment_reminders())
    try:
        await wait_for_enter("\n⏳ Enter를 누르면 반복 알림을 중단합니다 (브라우저 유지)... ")
    finally:
        reminder.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reminder
    print("🔕 반복 알림 중단. 브라우저에서 결제를 마무리하세요.")
