"""주문 결제 화면에서 결제 진입 버튼을 누르고 NOL 결제창 도착을 확인한다."""

import asyncio
import re
from urllib.parse import urlsplit

from patchright.async_api import expect, TimeoutError as BrowserTimeoutError

from preorder import is_payment_order_url


def is_payment_gateway_url(url):
    parsed = urlsplit(str(url))
    return (parsed.scheme == "https" and parsed.netloc == "nol-payment.yanolja.com"
            and parsed.path.rstrip("/") == "/payment")


# Next/React 화면은 button이 표시되고 enabled여도 아직 onClick이 연결되지 않을 수 있다.
# 클릭을 재시도하지 않고, 실제 React 핸들러가 연결된 후 사용자 클릭을 한 번만 보낸다.
_HAS_CLICK_HANDLER = """node => {
    const key = Object.keys(node).find(key => key.startsWith('__reactProps$'));
    return !!(key && typeof node[key]?.onClick === 'function');
}"""


async def click_payment_button(page, timeout_ms=20_000):
    """결제하러 가기를 1회 클릭하고 NOL 결제창 도착을 반환한다. 결제 완료는 아니다."""
    stage = "wait-payment-order"
    click_attempted = False
    try:
        async with asyncio.timeout(timeout_ms / 1000):
            await page.wait_for_url(
                lambda url: is_payment_order_url(url) or is_payment_gateway_url(url),
                wait_until="domcontentloaded", timeout=timeout_ms,
            )
            if is_payment_gateway_url(page.url):
                return {"ok": True, "reason": "already-payment-gateway"}
            stage = "wait-payment-button-ready"
            button = page.get_by_role("button", name="결제하러 가기", exact=True)
            await button.wait_for(state="visible", timeout=timeout_ms)
            await expect(button).to_be_enabled(timeout=timeout_ms)
            # Patchright의 기본 격리 컨텍스트에는 React가 붙인 DOM 속성이 보이지 않는다.
            while not await button.evaluate(_HAS_CLICK_HANDLER, isolated_context=False, timeout=timeout_ms):
                if not is_payment_order_url(page.url):
                    raise ValueError("결제 버튼 준비 중 화면이 변경되었습니다")
                await asyncio.sleep(0.1)
            await expect(button).to_be_enabled(timeout=timeout_ms)
            if not is_payment_order_url(page.url):
                raise ValueError("결제 버튼을 기다리는 사이 화면이 변경되었습니다")
            # 오류/응답 지연 시 같은 버튼을 재클릭하지 않는다.
            stage = "click-payment-button"
            click_attempted = True
            await button.click(timeout=timeout_ms, no_wait_after=True)
            stage = "wait-payment-gateway"
            await page.wait_for_url(
                is_payment_gateway_url, wait_until="domcontentloaded", timeout=timeout_ms,
            )
            return {"ok": True, "reason": "payment-gateway"}
    except Exception as exc:
        return {"ok": False, "reason": stage, "error_type": type(exc).__name__,
                "click_attempted": click_attempted, "requires_review": True}


_HAS_SELECTION_HANDLER = """node => {
    for (let element = node; element; element = element.parentElement) {
        const key = Object.keys(element).find(key => key.startsWith('__reactProps$'));
        const props = key && element[key];
        if (typeof props?.onChange === 'function' || typeof props?.onClick === 'function') return true;
    }
    return false;
}"""


async def click_gateway_payment_button(page, timeout_ms=20_000):
    """카카오페이 선택 후 1회 클릭하고 새 탭을 캡처한다. 결제 완료 판정은 하지 않는다."""
    stage = "wait-gateway-payment-page"
    click_attempted = False
    try:
        async with asyncio.timeout(timeout_ms / 1000):
            await page.wait_for_url(
                is_payment_gateway_url, wait_until="domcontentloaded", timeout=timeout_ms,
            )
            stage = "select-kakaopay"
            # 접근 가능한 이름 또는 카카오페이 문구와 같은 행의 라디오를 사용한다.
            # 단일 라디오가 있는 가장 가까운 조상만 허용해 다른 결제수단을 피한다.
            label = page.get_by_text("카카오페이", exact=True)
            row_radio = label.locator(
                "xpath=ancestor-or-self::*[count(.//input[@type='radio']) = 1][1]//input[@type='radio']"
            )
            radio = page.get_by_role("radio", name="카카오페이", exact=True).or_(row_radio)
            await radio.wait_for(state="attached", timeout=timeout_ms)
            if not await radio.is_checked():
                while not await radio.evaluate(_HAS_SELECTION_HANDLER, isolated_context=False, timeout=timeout_ms):
                    if not is_payment_gateway_url(page.url):
                        raise ValueError("결제수단 준비 중 화면이 변경되었습니다")
                    await asyncio.sleep(0.1)
                await expect(radio).to_be_enabled(timeout=timeout_ms)
                if not is_payment_gateway_url(page.url):
                    raise ValueError("결제수단 선택 전 화면이 변경되었습니다")
                if await radio.is_visible():
                    await radio.check(timeout=timeout_ms)
                else:
                    await label.click(timeout=timeout_ms)
            await expect(radio).to_be_checked(timeout=timeout_ms)
            stage = "wait-gateway-payment-button-ready"
            # 화면 크기에 따라 위치가 달라져도 금액이 붙은 결제 버튼만 찾는다.
            # 숨겨진 모바일 버튼은 제외하고, 보이는 후보가 여럿이면 클릭하지 않는다.
            button = page.get_by_role(
                "button", name=re.compile(r"^\s*[0-9]+(?:,[0-9]{3})*\s*원\s*결제하기\s*$"),
            )
            await button.wait_for(state="visible", timeout=timeout_ms)
            await expect(button).to_be_enabled(timeout=timeout_ms)
            while not await button.evaluate(_HAS_CLICK_HANDLER, isolated_context=False, timeout=timeout_ms):
                if not is_payment_gateway_url(page.url):
                    raise ValueError("결제 버튼 준비 중 화면이 변경되었습니다")
                await asyncio.sleep(0.1)
            await expect(button).to_be_enabled(timeout=timeout_ms)
            if not is_payment_gateway_url(page.url):
                raise ValueError("결제 버튼을 기다리는 사이 화면이 변경되었습니다")
            await expect(radio).to_be_checked(timeout=timeout_ms)
            stage = "click-gateway-payment-button"
            # 클릭 전에 새 탭 감시를 등록하여 즉시 열리는 팝업도 놓치지 않는다.
            async with page.expect_popup(timeout=timeout_ms) as popup_info:
                click_attempted = True
                await button.click(timeout=timeout_ms, no_wait_after=True)
                stage = "wait-kakaopay-popup"
            popup = await popup_info.value
            stage = "capture-kakaopay-popup"
            await popup.wait_for_load_state("load", timeout=timeout_ms)
            # 외부 인증 페이지의 지속 연결 때문에 networkidle은 보조 대기로만 사용한다.
            try:
                await popup.wait_for_load_state("networkidle", timeout=min(3000, timeout_ms))
            except BrowserTimeoutError:
                pass
            await popup.locator("body").wait_for(state="visible", timeout=timeout_ms)
            await popup.wait_for_function("""() =>
                (document.body.innerText.trim() || document.querySelector('img, canvas, iframe')) &&
                [...document.images].every(img => img.complete && img.naturalWidth > 0)
            """, timeout=timeout_ms)
            screenshot = await popup.screenshot(type="png", full_page=True, timeout=timeout_ms)
            return {"ok": True, "reason": "kakaopay-popup-captured", "screenshot": screenshot}
    except Exception as exc:
        return {"ok": False, "reason": stage, "error_type": type(exc).__name__,
                "click_attempted": click_attempted, "requires_review": True}
