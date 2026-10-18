"""로컬 Chromium에서 클릭 처리 준비와 NOL 결제창 도착을 검증한다."""

import json
import unittest

from patchright.async_api import async_playwright

from payment import click_gateway_payment_button, click_payment_button, is_payment_gateway_url


ORDER_URL = 'https://tickets.interpark.com/payment/order/test-order'
GATEWAY_URL = 'https://nol-payment.yanolja.com/payment'
HTML = """<!doctype html><meta charset="utf-8"><h2>결제 준비</h2>
<button id="coupon">쿠폰 할인</button>
<aside><h2>결제 금액</h2><button id="pay">결제하러 가기</button></aside>
<script>
const config = CONFIG;
sessionStorage.setItem('clicks', '0');
sessionStorage.setItem('coupons', '0');
const pay = document.querySelector('#pay');
pay.disabled = !!config.disabled;
pay.addEventListener('click', () => {
  sessionStorage.setItem('clicks', String(Number(sessionStorage.getItem('clicks')) + 1));
  window.recordClick = fetch('/test-events', {method: 'POST', body: 'payment'});
});
const handler = async () => {
  await window.recordClick;
  document.body.insertAdjacentHTML('beforeend', '<p>후속 안내</p>');
  if (!config.noNavigation) location.assign(config.errorNavigation
    ? '/payment/error' : 'https://nol-payment.yanolja.com/payment');
};
const hydrate = () => {
  pay.__reactProps$test = {onClick: handler};
  pay.onclick = handler;
};
if (!config.neverHydrate) {
  if (config.hydrationDelay) setTimeout(hydrate, config.hydrationDelay);
  else hydrate();
}
document.querySelector('#coupon').onclick = () => fetch('/test-events', {method: 'POST', body: 'coupon'});
if (config.delayed) {
  pay.hidden = true; pay.disabled = true;
  setTimeout(() => { pay.hidden = false; pay.disabled = false; }, 200);
}
if (config.duplicate) {
  document.body.append(pay.cloneNode(true));
}
if (config.hiddenDuplicate) {
  const hidden = pay.cloneNode(true); hidden.hidden = true; document.body.append(hidden);
}
</script>"""


GATEWAY_HTML = HTML.replace('결제하러 가기', '18,000원 결제하기').replace(
    "const config = CONFIG;", "const config = CONFIG; config.noNavigation = true;").replace(
    '<script>', '''<label><input type="radio" name="method" id="npay" checked>Npay</label>
<label><input type="radio" name="method" id="kakao">카카오페이</label><script>''').replace(
    'const handler = async () => {', '''const handler = async () => {
  window.paymentMethodAtClick = document.querySelector('input:checked').id;
  if (!config.noPopup) window.open('https://pay.example.test/auth');''').replace(
    '</script>', '''
const kakao = document.querySelector('#kakao');
kakao.__reactProps$test = {onChange: () => {}};
kakao.disabled = !!config.kakaoDisabled;
if (config.kakaoSelected) kakao.checked = true;
if (config.kakaoHidden) kakao.style.display = 'none';
if (config.kakaoMissing) kakao.parentElement.remove();
if (config.kakaoReject) kakao.addEventListener('click', e => e.preventDefault());
</script>''')


class PaymentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.driver = await async_playwright().start()
        self.browser = await self.driver.chromium.launch(headless=True)
        self.page = await self.browser.new_page()
        self.clicks = 0
        self.coupon_clicks = 0

    async def asyncTearDown(self):
        await self.browser.close()
        await self.driver.stop()

    async def load(self, url=ORDER_URL, **config):
        async def intercept(route):
            # Patchright는 페이지 console 이벤트를 전달하지 않으므로 로컬 요청으로 횟수 수집.
            if route.request.url.endswith('/test-events'):
                self.clicks += route.request.post_data == 'payment'
                self.coupon_clicks += route.request.post_data == 'coupon'
                await route.fulfill(status=200, body='ok')
                return
            if route.request.url == 'https://pay.example.test/auth':
                body = '<html><body><h1>카카오페이 인증</h1><p>테스트 결제 화면</p></body></html>'
            elif is_payment_gateway_url(route.request.url):
                body = (GATEWAY_HTML.replace('CONFIG', json.dumps(config))
                        if config.get('gatewayButton') else '<h1>NOL 결제</h1>')
            elif route.request.url.endswith('/payment/error'):
                body = '<h1>결제 오류</h1>'
            else:
                body = HTML.replace('CONFIG', json.dumps(config))
            await route.fulfill(status=200, content_type='text/html; charset=utf-8',
                                body=body)
        await self.page.context.route('**/*', intercept)
        await self.page.goto(url)

    async def assert_clicks(self, count):
        self.assertEqual(self.clicks, count)
        self.assertEqual(self.coupon_clicks, 0)

    async def test_clicks_once_without_claiming_payment_completed(self):
        await self.load()
        result = await click_payment_button(self.page)
        self.assertEqual(result, {'ok': True, 'reason': 'payment-gateway'})
        self.assertEqual(self.page.url, GATEWAY_URL)
        await self.assert_clicks(1)

    async def test_waits_for_visible_enabled_button(self):
        await self.load(delayed=True)
        self.assertTrue((await click_payment_button(self.page))['ok'])
        await self.assert_clicks(1)

    async def test_disabled_button_requires_review_without_click(self):
        await self.load(disabled=True)
        result = await click_payment_button(self.page, timeout_ms=1000)
        self.assertFalse(result['ok'])
        self.assertTrue(result['requires_review'])
        await self.assert_clicks(0)

    async def test_wrong_page_does_not_click_matching_button(self):
        await self.load(url='https://tickets.interpark.com/onestop/error')
        result = await click_payment_button(self.page, timeout_ms=1000)
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'wait-payment-order')
        await self.assert_clicks(0)

    async def test_ambiguous_buttons_are_not_clicked(self):
        await self.load(duplicate=True)
        self.assertFalse((await click_payment_button(self.page))['ok'])
        await self.assert_clicks(0)

    async def test_hidden_mobile_duplicate_is_ignored(self):
        await self.load(hiddenDuplicate=True)
        self.assertTrue((await click_payment_button(self.page))['ok'])
        await self.assert_clicks(1)

    async def test_waits_for_hydration_before_first_click(self):
        # 버튼이 처음부터 visible/enabled이지만 이벤트 핸들러만 늦게 연결되는 회귀 사례.
        await self.load(hydrationDelay=400)
        self.assertTrue((await click_payment_button(self.page))['ok'])
        self.assertEqual(self.page.url, GATEWAY_URL)
        await self.assert_clicks(1)

    async def test_never_hydrated_button_is_not_clicked(self):
        await self.load(neverHydrate=True)
        result = await click_payment_button(self.page, timeout_ms=1000)
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'wait-payment-button-ready')
        self.assertFalse(result['click_attempted'])
        await self.assert_clicks(0)

    async def test_click_without_navigation_is_not_success_or_retried(self):
        await self.load(noNavigation=True)
        result = await click_payment_button(self.page, timeout_ms=1000)
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'wait-payment-gateway')
        self.assertTrue(result['click_attempted'])
        await self.assert_clicks(1)

    async def test_error_page_is_not_payment_gateway(self):
        await self.load(errorNavigation=True)
        result = await click_payment_button(self.page, timeout_ms=1000)
        self.assertFalse(result['ok'])
        await self.assert_clicks(1)

    async def test_already_gateway_does_not_click_again(self):
        await self.load(url=GATEWAY_URL)
        result = await click_payment_button(self.page)
        self.assertEqual(result, {'ok': True, 'reason': 'already-payment-gateway'})
        await self.assert_clicks(0)

    async def test_gateway_clicks_amount_button_once(self):
        await self.load(url=GATEWAY_URL, gatewayButton=True)
        result = await click_gateway_payment_button(self.page)
        self.assertTrue(result['ok'])
        self.assertEqual(result['reason'], 'kakaopay-popup-captured')
        self.assertTrue(result['screenshot'].startswith(b'\x89PNG\r\n\x1a\n'))
        self.assertEqual(await self.page.evaluate('window.paymentMethodAtClick', isolated_context=False), 'kakao')
        self.assertEqual(len(self.page.context.pages), 2)
        await self.page.evaluate('window.recordClick', isolated_context=False)
        await self.assert_clicks(1)

    async def test_gateway_accepts_amounts_and_whitespace(self):
        for label in ('1000원 결제하기', '1,234,500 원 결제하기', '0원 결제하기'):
            with self.subTest(label=label):
                await self.load(url=GATEWAY_URL, gatewayButton=True)
                await self.page.locator('#pay').evaluate('(node, text) => node.textContent = text', label)
                self.assertTrue((await click_gateway_payment_button(self.page))['ok'])
                await self.page.evaluate('window.recordClick', isolated_context=False)
        await self.assert_clicks(3)

    async def test_gateway_waits_for_delayed_hydration_and_enabled_button(self):
        await self.load(url=GATEWAY_URL, gatewayButton=True, delayed=True, hydrationDelay=400)
        self.assertTrue((await click_gateway_payment_button(self.page))['ok'])
        await self.page.evaluate('window.recordClick', isolated_context=False)
        await self.assert_clicks(1)

    async def test_gateway_hidden_mobile_button_is_ignored(self):
        await self.load(url=GATEWAY_URL, gatewayButton=True, hiddenDuplicate=True)
        self.assertTrue((await click_gateway_payment_button(self.page))['ok'])
        await self.page.evaluate('window.recordClick', isolated_context=False)
        await self.assert_clicks(1)

    async def test_gateway_unready_or_ambiguous_buttons_are_not_clicked(self):
        for config in ({'disabled': True}, {'neverHydrate': True}, {'duplicate': True}):
            with self.subTest(config=config):
                await self.load(url=GATEWAY_URL, gatewayButton=True, **config)
                result = await click_gateway_payment_button(self.page, timeout_ms=700)
                self.assertFalse(result['ok'])
                self.assertFalse(result['click_attempted'])
                self.assertTrue(result['requires_review'])
        await self.assert_clicks(0)

    async def test_gateway_wrong_page_does_not_click(self):
        await self.load()
        result = await click_gateway_payment_button(self.page, timeout_ms=700)
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'wait-gateway-payment-page')
        await self.assert_clicks(0)

    async def test_gateway_missing_amount_does_not_click(self):
        await self.load(url=GATEWAY_URL, gatewayButton=True)
        await self.page.locator('#pay').evaluate("node => node.textContent = '결제하기'")
        result = await click_gateway_payment_button(self.page, timeout_ms=700)
        self.assertFalse(result['ok'])
        await self.assert_clicks(0)

    async def test_order_then_gateway_clicks_both_buttons(self):
        await self.load(gatewayButton=True)
        self.assertTrue((await click_payment_button(self.page))['ok'])
        self.assertTrue((await click_gateway_payment_button(self.page))['ok'])
        await self.page.evaluate('window.recordClick', isolated_context=False)
        await self.assert_clicks(2)

    async def test_kakao_selection_failure_never_clicks_payment(self):
        for config in ({'kakaoDisabled': True}, {'kakaoMissing': True}, {'kakaoReject': True}):
            with self.subTest(config=config):
                await self.load(url=GATEWAY_URL, gatewayButton=True, **config)
                result = await click_gateway_payment_button(self.page, timeout_ms=700)
                self.assertFalse(result['ok'])
                self.assertEqual(result['reason'], 'select-kakaopay')
                self.assertFalse(result['click_attempted'])
        await self.assert_clicks(0)

    async def test_kakao_already_selected_or_hidden_radio(self):
        for config in ({'kakaoSelected': True}, {'kakaoHidden': True}):
            with self.subTest(config=config):
                await self.load(url=GATEWAY_URL, gatewayButton=True, **config)
                self.assertTrue((await click_gateway_payment_button(self.page))['ok'])
                self.assertEqual(await self.page.evaluate('window.paymentMethodAtClick', isolated_context=False), 'kakao')
        await self.assert_clicks(2)

    async def test_missing_popup_does_not_retry_payment_click(self):
        await self.load(url=GATEWAY_URL, gatewayButton=True, noPopup=True)
        result = await click_gateway_payment_button(self.page, timeout_ms=1000)
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'wait-kakaopay-popup')
        self.assertTrue(result['click_attempted'])
        await self.assert_clicks(1)


class GatewayUrlTests(unittest.TestCase):
    def test_accepts_only_exact_gateway_origin_and_path(self):
        self.assertTrue(is_payment_gateway_url(GATEWAY_URL + '?service=yanolja'))
        for url in (ORDER_URL, GATEWAY_URL + '/error',
                    GATEWAY_URL.replace('nol-payment.yanolja.com', 'example.com')):
            self.assertFalse(is_payment_gateway_url(url))


if __name__ == '__main__':
    unittest.main()
