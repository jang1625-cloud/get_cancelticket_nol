"""외부 통신 없이 실제 Chromium에서 전체 동의와 예매 진행을 검증한다."""

import json
import unittest

from patchright.async_api import async_playwright

from preorder import agree_and_continue, is_payment_order_url


PREORDER_URL = "https://tickets.interpark.com/onestop/preorder"
PAYMENT_URL = "https://tickets.interpark.com/payment/order/test-order"
HTML = """<!doctype html><meta charset="utf-8">
<style>label input { display: none; } label { display: inline-block; padding: 10px; }</style>
<h2>예매 정보 입력</h2><h2>약관 동의</h2>
<label><input id="all" type="checkbox"><i></i><span>전체 동의</span></label>
<label><input id="required" type="checkbox"><span>필수 약관</span></label>
<button id="submit" disabled>예매 진행하기</button>
<script>
const config = CONFIG;
const all = document.querySelector('#all');
const required = document.querySelector('#required');
const submit = document.querySelector('#submit');
let changes = 0, submits = 0;
function record() {
  sessionStorage.setItem('counts', JSON.stringify({changes, submits}));
}
function update() {
  required.checked = all.checked;
  submit.disabled = config.disabled || !all.checked;
}
all.checked = !!config.checked;
all.onchange = () => {
  changes++; record();
  if (config.rejectAgreement) all.checked = false;
  setTimeout(update, 30);
};
submit.onclick = () => {
  submits++; record();
  if (config.noNavigation) {
    document.body.insertAdjacentHTML('beforeend', '<p role="alert">예매자 정보를 확인해주세요</p>');
  } else {
    location.assign(config.errorNavigation ? '/onestop/error' : '/payment/order/test-order');
  }
};
update(); record();
</script>"""


class PreorderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.driver = await async_playwright().start()
        self.browser = await self.driver.chromium.launch(headless=True)
        self.page = await self.browser.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.driver.stop()

    async def load(self, **config):
        async def intercept(route):
            url = route.request.url
            if is_payment_order_url(url):
                body = '<h2>티켓 결제</h2><button>결제하기</button>'
            elif url.endswith('/error'):
                body = '<h2>오류</h2>'
            else:
                body = HTML.replace('CONFIG', json.dumps(config))
            await route.fulfill(status=200, content_type='text/html; charset=utf-8', body=body)
        await self.page.route('**/*', intercept)
        await self.page.goto(PREORDER_URL)

    async def assert_counts(self, changes, submits):
        counts = await self.page.evaluate("JSON.parse(sessionStorage.getItem('counts'))")
        self.assertEqual(counts, {'changes': changes, 'submits': submits})

    async def test_agrees_and_reaches_payment(self):
        await self.load()
        result = await agree_and_continue(self.page)
        self.assertTrue(result['ok'])
        self.assertEqual(self.page.url, PAYMENT_URL)
        await self.assert_counts(1, 1)

    async def test_existing_agreement_is_not_unchecked(self):
        await self.load(checked=True)
        self.assertTrue((await agree_and_continue(self.page))['ok'])
        await self.assert_counts(0, 1)

    async def test_disabled_button_is_never_clicked(self):
        await self.load(disabled=True)
        result = await agree_and_continue(self.page, timeout_ms=1500)
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'submit-preorder')
        await self.assert_counts(1, 0)

    async def test_unconfirmed_agreement_does_not_submit(self):
        await self.load(rejectAgreement=True)
        result = await agree_and_continue(self.page, timeout_ms=1500)
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'agree-all')
        await self.assert_counts(1, 0)

    async def test_validation_error_never_resubmits(self):
        await self.load(noNavigation=True)
        result = await agree_and_continue(self.page, timeout_ms=1500)
        self.assertFalse(result['ok'])
        self.assertTrue(result['requires_review'])
        self.assertEqual(result['reason'], 'wait-payment')
        await self.assert_counts(1, 1)

    async def test_error_redirect_is_not_success(self):
        await self.load(errorNavigation=True)
        result = await agree_and_continue(self.page, timeout_ms=1500)
        self.assertFalse(result['ok'])
        await self.assert_counts(1, 1)

    async def test_already_payment_does_not_submit_again(self):
        await self.load()
        await self.page.goto(PAYMENT_URL)
        self.assertEqual((await agree_and_continue(self.page))['reason'], 'already-payment')
        await self.assert_counts(0, 0)


class PaymentUrlTests(unittest.TestCase):
    def test_only_order_page_is_accepted(self):
        self.assertTrue(is_payment_order_url(PAYMENT_URL + '?test=1'))
        for url in (PREORDER_URL, 'https://tickets.interpark.com/onestop/error',
                    'https://tickets.interpark.com/payment/order/',
                    PAYMENT_URL + '/other', PAYMENT_URL.replace('tickets.interpark.com', 'example.com')):
            self.assertFalse(is_payment_order_url(url))


if __name__ == '__main__':
    unittest.main()
