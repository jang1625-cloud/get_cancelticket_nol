"""확보한 좌석의 최상단 가격을 선택하고 예매 정보 입력 화면까지 진행한다."""

import asyncio
import re
from urllib.parse import parse_qs, urlsplit

from patchright.async_api import expect


# 저장된 onestop 프런트엔드의 PriceGroup/PriceItem/Stepper 컴포넌트 기준.
# CSS Modules의 빌드별 해시는 제외한다. +/- 버튼은 aria-hidden이므로 CSS로 찾는다.
PRICE_GROUP = '[class*="PriceGroup_group__"]:visible'
PRICE_ITEM = 'li[class*="PriceItem_typeItem__"]:visible'
QUANTITY_INPUT = 'input[role="spinbutton"]'
INCREMENT_BUTTON = 'button[class*="stepper__incrementButton"]'


def is_preorder_url(url):
    parsed = urlsplit(str(url))
    return (parsed.scheme == "https" and parsed.netloc == "tickets.interpark.com"
            and parsed.path.rstrip("/") == "/onestop/preorder")


def is_price_url(url):
    parsed = urlsplit(str(url))
    return (parsed.scheme == "https" and parsed.netloc == "tickets.interpark.com"
            and parsed.path == "/onestop/seat"
            and parse_qs(parsed.query).get("step") == ["price"])


async def select_prices_and_continue(page, ticket_count, timeout_ms=20_000):
    """가격 선택 후 preorder 도착 여부를 반환한다. 실패 시 재클릭/재예약하지 않는다.

    ticket_count는 설정 최대 매수가 아닌 실제 확보한 좌석 수다.
    기존에 다른 가격이 선택되어 있거나 화면 수량이 다르면 수동 확인으로 넘긴다.
    """
    stage = "wait-price-page"
    try:
        if not isinstance(ticket_count, int) or isinstance(ticket_count, bool) or ticket_count < 1:
            raise ValueError("확보한 좌석 수가 올바르지 않습니다")
        # 여러 개의 대기/클릭을 포함한 전체 단계의 제한 시간.
        async with asyncio.timeout(timeout_ms / 1000):
            await page.wait_for_url(
                lambda url: is_price_url(url) or is_preorder_url(url),
                wait_until="domcontentloaded", timeout=timeout_ms,
            )
            if is_preorder_url(page.url):
                return {"ok": True, "reason": "already-preorder", "ticket_count": ticket_count}

            stage = "check-price-quantities"
            groups = page.locator(PRICE_GROUP)
            await groups.first.wait_for(state="visible", timeout=timeout_ms)
            targets = []
            for group in await groups.all():
                maximum = await group.locator('[class*="PriceGroup_countMax__"]').inner_text()
                match = re.fullmatch(r"\s*/\s*(\d+)\s*", maximum)
                if not match or int(match[1]) < 1:
                    raise ValueError("가격 그룹의 확보 수량을 확인할 수 없습니다")
                target = int(match[1])
                rows = group.locator(PRICE_ITEM)
                await rows.first.wait_for(state="visible", timeout=timeout_ms)
                # 다른 가격을 임의로 해제하거나 다음 가격으로 대체하지 않는다.
                for row in (await rows.all())[1:]:
                    if int(await row.locator(QUANTITY_INPUT).input_value()) != 0:
                        raise ValueError("최상단 이외의 가격에 수량이 선택되어 있습니다")
                top = rows.first
                current = int(await top.locator(QUANTITY_INPUT).input_value())
                if not 0 <= current <= target:
                    raise ValueError("선택된 가격 수량이 확보 수량과 다릅니다")
                targets.append((group, top, target))
            if sum(target for _, _, target in targets) != ticket_count:
                raise ValueError("가격 화면과 실제 확보한 좌석 수가 다릅니다")

            stage = "select-top-price"
            for group, top, target in targets:
                quantity = top.locator(QUANTITY_INPUT)
                current = int(await quantity.input_value())
                # 상품별 증가 단위가 달라도 매번 실제 반영된 수량을 읽는다.
                for _ in range(target):
                    if current == target:
                        break
                    plus = top.locator(INCREMENT_BUTTON)
                    await expect(plus).to_be_enabled(timeout=timeout_ms)
                    await plus.click(timeout=timeout_ms)
                    await expect(quantity).not_to_have_value(str(current), timeout=timeout_ms)
                    updated = int(await quantity.input_value())
                    if not current < updated <= target:
                        raise ValueError("가격 수량 증가 결과가 예상 범위를 벗어났습니다")
                    current = updated
                await expect(quantity).to_have_value(str(target), timeout=timeout_ms)
                await expect(group.locator('[class*="PriceGroup_countCurrent__"]')).to_have_text(
                    str(target), timeout=timeout_ms,
                )

            stage = "submit-prices"
            # 클릭 직전에도 최상단 가격에만 정확한 수량이 선택됐는지 확인한다.
            for group, top, target in targets:
                values = [int(await value.input_value())
                          for value in await group.locator(QUANTITY_INPUT).all()]
                if not values or values[0] != target or any(values[1:]):
                    raise ValueError("진행 직전 가격 선택 상태가 변경되었습니다")
            if not is_price_url(page.url):
                raise ValueError("가격 선택 도중 화면이 변경되었습니다")
            submit = page.get_by_role("button", name=re.compile(rf"^\s*{ticket_count}\s*매\s*예매하기\s*$"))
            await expect(submit).to_be_enabled(timeout=timeout_ms)
            # 한 번만 클릭한다. 응답 지연/실패 시 중복 제출하지 않는다.
            await submit.click(timeout=timeout_ms)
            stage = "wait-preorder"
            await page.wait_for_url(is_preorder_url, wait_until="domcontentloaded", timeout=timeout_ms)
            return {"ok": True, "reason": "preorder", "ticket_count": ticket_count}
    except Exception as exc:
        return {"ok": False, "reason": stage, "error_type": type(exc).__name__,
                "requires_review": True}
