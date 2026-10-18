"""실제 Chromium + 로컬 HTML로 수량 클릭과 이동을 검증한다. 외부 요청은 모두 차단한다."""

import json
import unittest

from patchright.async_api import async_playwright

from price_selection import is_preorder_url, is_price_url, select_prices_and_continue


PRICE_URL = "https://tickets.interpark.com/onestop/seat?step=price"
PREORDER_URL = "https://tickets.interpark.com/onestop/preorder"

# 저장된 프런트엔드의 DOM 구조: 그룹별 가격 목록, readonly spinbutton,
# aria-hidden SVG 증가 버튼. 해시는 실제 저장본과 다르게 지정한다.
HTML = """<!doctype html><meta charset="utf-8"><h2>가격 선택</h2>
<main id="groups"></main><button id="submit"></button>
<script>
const config = CONFIG;
window.plusClicks = 0; window.submitClicks = 0;
function recordClicks() {
  sessionStorage.setItem('counts', JSON.stringify({plus: window.plusClicks, submit: window.submitClicks}));
}
recordClicks();
const groups = document.querySelector('#groups');
config.groups.forEach(item => {
  const group = document.createElement('div');
  group.className = 'PriceGroup_group__test';
  group.innerHTML = `<span class="PriceGroup_countCurrent__test">0</span>
    <span class="PriceGroup_countMax__test">/${item.target}</span><ul></ul>`;
  [item.current || 0, item.other || 0].forEach((value, index) => {
    const row = document.createElement('li');
    row.className = 'PriceItem_typeItem__test';
    row.innerHTML = `<div>${index ? '할인 가격' : '예매가/성인'}</div>
      <div class="stepper__root"><button aria-hidden="true">−</button>
      <input role="spinbutton" readonly value="${value}">
      <button class="stepper__incrementButton" aria-hidden="true"><svg></svg></button></div>`;
    const plus = row.querySelector('.stepper__incrementButton');
    plus.onclick = () => {
      window.plusClicks++;
      recordClicks();
      if (config.noChange) return;
      setTimeout(() => {
        const input = row.querySelector('input');
        input.value = Number(input.value) + (config.step || 1);
        update();
      }, 30);
    };
    group.querySelector('ul').append(row);
  });
  groups.append(group);
});
function update() {
  let total = 0;
  [...groups.children].forEach((group, i) => {
    const count = [...group.querySelectorAll('input')].reduce((sum, input) => sum + Number(input.value), 0);
    group.querySelector('[class*="countCurrent"]').textContent = count;
    group.querySelectorAll('.stepper__incrementButton').forEach(button => {
      button.disabled = config.disabled || count >= config.groups[i].target;
    });
    total += count;
  });
  document.querySelector('#submit').textContent = total + '매 예매하기';
}
document.querySelector('#submit').onclick = () => {
  window.submitClicks++;
  recordClicks();
  if (!config.noNavigation) location.assign('/onestop/preorder');
};
update();
</script>"""


class PriceSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.driver = await async_playwright().start()
        self.browser = await self.driver.chromium.launch(headless=True)
        self.page = await self.browser.new_page()
        self.requests = []

    async def asyncTearDown(self):
        await self.browser.close()
        await self.driver.stop()

    async def load(self, groups, **options):
        config = {"groups": groups, **options}

        async def intercept(route):
            self.requests.append(route.request.url)
            body = ("<h2>예매 정보 입력</h2>" if is_preorder_url(route.request.url)
                    else HTML.replace("CONFIG", json.dumps(config)))
            await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)

        await self.page.route("**/*", intercept)
        await self.page.goto(PRICE_URL)

    async def assert_counts(self, plus, submit):
        # Patchright의 evaluate 격리 컨텍스트와 페이지 스크립트 양쪽에서 읽을 수 있는 저장소.
        counts = await self.page.evaluate("JSON.parse(sessionStorage.getItem('counts'))")
        self.assertEqual(counts, {"plus": plus, "submit": submit})

    async def test_selects_top_price_only_and_reaches_preorder(self):
        await self.load([{"target": 4}])
        result = await select_prices_and_continue(self.page, 4)
        self.assertTrue(result["ok"])
        self.assertEqual(self.page.url, PREORDER_URL)
        await self.assert_counts(4, 1)

    async def test_partial_selection_clicks_only_missing_quantity(self):
        await self.load([{"target": 3, "current": 1}])
        self.assertTrue((await select_prices_and_continue(self.page, 3))["ok"])
        await self.assert_counts(2, 1)

    async def test_complete_selection_skips_disabled_plus(self):
        await self.load([{"target": 1, "current": 1}])
        self.assertTrue((await select_prices_and_continue(self.page, 1))["ok"])
        await self.assert_counts(0, 1)

    async def test_group_quantities_and_product_increment(self):
        await self.load([{"target": 2}, {"target": 2}], step=2)
        self.assertTrue((await select_prices_and_continue(self.page, 4))["ok"])
        await self.assert_counts(2, 1)

    async def test_mismatched_total_does_not_change_selection(self):
        await self.load([{"target": 2}])
        result = await select_prices_and_continue(self.page, 4)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "check-price-quantities")
        await self.assert_counts(0, 0)

    async def test_other_price_selection_requires_review(self):
        await self.load([{"target": 2, "other": 1}])
        result = await select_prices_and_continue(self.page, 2)
        self.assertTrue(result["requires_review"])
        await self.assert_counts(0, 0)

    async def test_disabled_top_price_does_not_choose_discount(self):
        await self.load([{"target": 2}], disabled=True)
        result = await select_prices_and_continue(self.page, 2, timeout_ms=800)
        self.assertFalse(result["ok"])
        await self.assert_counts(0, 0)

    async def test_quantity_validation_modal_does_not_repeat_click(self):
        await self.load([{"target": 2}], noChange=True)
        result = await select_prices_and_continue(self.page, 2, timeout_ms=1000)
        self.assertFalse(result["ok"])
        await self.assert_counts(1, 0)

    async def test_failed_navigation_never_submits_twice(self):
        await self.load([{"target": 1, "current": 1}], noNavigation=True)
        result = await select_prices_and_continue(self.page, 1, timeout_ms=1000)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "wait-preorder")
        await self.assert_counts(0, 1)

    async def test_already_preorder_is_noop(self):
        await self.load([{"target": 1}])
        await self.page.goto(PREORDER_URL)
        result = await select_prices_and_continue(self.page, 1)
        self.assertEqual(result["reason"], "already-preorder")
        self.assertEqual(len(self.requests), 2)


class UrlTests(unittest.TestCase):
    def test_exact_origin_path_and_price_step(self):
        self.assertTrue(is_price_url(PRICE_URL + '&foo=bar'))
        self.assertFalse(is_price_url(PRICE_URL.replace('price', 'seat')))
        self.assertTrue(is_preorder_url(PREORDER_URL + '?foo=bar'))
        self.assertFalse(is_preorder_url(PREORDER_URL + '/other'))
        self.assertFalse(is_preorder_url(PREORDER_URL.replace('tickets.interpark.com', 'example.com')))


if __name__ == "__main__":
    unittest.main()
