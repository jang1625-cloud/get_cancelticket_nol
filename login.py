import asyncio
import os
import sys

from patchright.async_api import async_playwright
from browser_config import SESSION_PATH, get_launch_options

LOGIN_URL = "https://nol.yanolja.com/"

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

        # 기존 세션이 있으면 이어받아 띄움 (없으면 새 세션)
        options = dict(CONTEXT_OPTIONS)
        if os.path.exists(SESSION_PATH):
            options["storage_state"] = SESSION_PATH
            print(f"📂 세션 파일 사용(로그인 유효 여부는 별도 확인): {SESSION_PATH}")
        else:
            print(f"ℹ️ 세션 파일 없음 — 새 세션 시작: {SESSION_PATH}")

        context = await browser.new_context(**options)
        page = await context.new_page()

        print(f"🌐 접속: {LOGIN_URL}")
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)

        print("\n👉 브라우저에서 직접 로그인하세요.")
        await wait_for_enter("   로그인 완료 후 이 터미널에서 Enter 를 누르세요... ")

        await context.storage_state(path=SESSION_PATH)
        print(f"✅ 세션 저장 완료: {SESSION_PATH}")

        await context.close()
        await browser.close()
        print("🔚 브라우저 종료")


if __name__ == "__main__":
    asyncio.run(main())
