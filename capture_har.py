import asyncio
import os

from patchright.async_api import async_playwright
from browser_config import SESSION_PATH, get_launch_options

HAR_PATH = "booking_capture.har"

# 예매를 시작할 페이지 (필요하면 실제 예매 상세/좌석 페이지 URL 로 바꿔도 됨)
START_URL = "https://nol.yanolja.com/ticket/genre/sports/bears"

CONTEXT_OPTIONS = {
    "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "viewport": {"width": 1280, "height": 800},
    "locale": "ko-KR",
    "timezone_id": "Asia/Seoul",
}


async def wait_for_enter(prompt: str):
    """터미널 입력(Enter)을 비동기로 대기."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, input, prompt)


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(**get_launch_options())

        options = dict(CONTEXT_OPTIONS)
        if os.path.exists(SESSION_PATH):
            options["storage_state"] = SESSION_PATH
            print(f"📂 세션 파일 사용(로그인 유효 여부는 별도 확인): {SESSION_PATH}")
        else:
            print(f"ℹ️ 세션 파일 없음 — 새 세션 시작: {SESSION_PATH}")

        # HAR 기록 활성화: 요청/응답 본문까지 모두 저장한다.
        options["record_har_path"] = HAR_PATH
        options["record_har_mode"] = "full"       # 본문 포함(full) — 좌석/예매 API 페이로드 확인용
        options["record_har_content"] = "embed"   # 응답 본문을 HAR 안에 직접 임베드

        context = await browser.new_context(**options)
        page = await context.new_page()

        print(f"🌐 접속: {START_URL}")
        await page.goto(START_URL, wait_until="domcontentloaded", timeout=30_000)

        print("\n" + "=" * 60)
        print("👉 브라우저에서 실제 예매 플로우를 '수동으로' 한 번 진행하세요.")
        print("   (경기 선택 → 예매하기 → 좌석 선택 → 결제 직전까지)")
        print("   모든 네트워크 요청/응답이 HAR 로 기록됩니다.")
        print("=" * 60 + "\n")
        await wait_for_enter("   플로우를 마친 뒤 이 터미널에서 Enter 를 누르세요... ")

        # 세션도 함께 갱신 저장
        await context.storage_state(path=SESSION_PATH)
        print(f"✅ 세션 저장 완료: {SESSION_PATH}")

        # context.close() 시점에 HAR 파일이 flush 되어 기록된다.
        await context.close()
        await browser.close()
        print(f"✅ HAR 저장 완료: {HAR_PATH}")
        print("🔚 브라우저 종료")


if __name__ == "__main__":
    asyncio.run(main())
