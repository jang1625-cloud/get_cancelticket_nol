"""예매 정보 화면에서 전체 약관에 동의하고 결제 화면까지 진행한다."""

import asyncio
import re
from urllib.parse import urlsplit

from patchright.async_api import expect

from price_selection import is_preorder_url


def is_payment_order_url(url):
    parsed = urlsplit(str(url))
    return (parsed.scheme == "https" and parsed.netloc == "tickets.interpark.com"
            and re.fullmatch(r"/payment/order/[^/]+/?", parsed.path) is not None)


async def agree_and_continue(page, timeout_ms=20_000):
    """전체 동의 → 예매 진행하기 1회 클릭 → 결제 화면 확인. 실패 시 재제출하지 않는다."""
    stage = "wait-preorder"
    try:
        async with asyncio.timeout(timeout_ms / 1000):
            await page.wait_for_url(
                lambda url: is_preorder_url(url) or is_payment_order_url(url),
                wait_until="domcontentloaded", timeout=timeout_ms,
            )
            if is_payment_order_url(page.url):
                return {"ok": True, "reason": "already-payment"}

            stage = "agree-all"
            # 실제 컴포넌트: label > input[type=checkbox] + i + span(전체 동의).
            # 꾸며진 체크박스의 input이 숨겨져 있어도 사용자에게 보이는 label은 클릭할 수 있다.
            label = page.locator("label").filter(has=page.get_by_text("전체 동의", exact=True))
            await label.wait_for(state="visible", timeout=timeout_ms)
            checkbox = page.get_by_label("전체 동의", exact=True)
            if not await checkbox.is_checked():
                await label.click(timeout=timeout_ms)
            await expect(checkbox).to_be_checked(timeout=timeout_ms)

            stage = "submit-preorder"
            submit = page.get_by_role("button", name="예매 진행하기", exact=True)
            await expect(submit).to_be_enabled(timeout=timeout_ms)
            # 기다리는 사이 동의가 해제되거나 다른 화면으로 이동했으면 제출하지 않는다.
            await expect(checkbox).to_be_checked(timeout=timeout_ms)
            if not is_preorder_url(page.url):
                raise ValueError("약관 동의 도중 화면이 변경되었습니다")
            await submit.click(timeout=timeout_ms)
            stage = "wait-payment"
            # 공개된 preorder 프런트엔드가 성공 응답을 받은 후 이동하는 경로.
            await page.wait_for_url(is_payment_order_url, wait_until="domcontentloaded", timeout=timeout_ms)
            return {"ok": True, "reason": "payment"}
    except Exception as exc:
        return {"ok": False, "reason": stage, "error_type": type(exc).__name__,
                "requires_review": True}
