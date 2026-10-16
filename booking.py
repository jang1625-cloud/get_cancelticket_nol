"""NOL(야놀자)·인터파크 원스톱 야구 예매 자동화 (API + axios 인터셉터 재현).

핵심(왜 이 방식인가):
  - 좌석 개별 <circle> 은 블록을 '확대'해야 DOM 에 렌더되므로, 확대 없이 클릭할 요소가
    아예 없다. 그래서 클릭 방식 대신 서버 API(preselect → seats/select)를 직접 부른다.
  - 단, seats/select 를 그냥 fetch 하면 400 이 난다. 프론트 JS(9732 청크) 분석 결과,
    프론트는 axios 인스턴스의 request 인터셉터가 sessionStorage["interpark/context"]
    를 읽어 매 요청에 헤더를 심는다. 이 헤더(특히 매 요청 랜덤인 X-OneStop-Trace-ID)가
    빠져서 400 이었다. → 인터셉터를 그대로 재현하면 확대/클릭 없이 API 로 선택된다.

인터셉터가 심는 헤더 (interpark/context 에서 읽음):
    X-OneStop-Trace-ID    : 매 요청 랜덤 16자
    X-Onestop-Channel     : ctx.channelType || "ONESTOP"
    X-Onestop-Session     : ctx.sessionId
    X-Ticket-BFF-Language : 언어맵[ctx.lang]  (ko → "KO")
    Authorization         : "Bearer " + ctx.goods.preOpt (있을 때)

흐름:
  - 대기열/캡차/로그인 등은 실제 브라우저가 처리(사람).
  - 좌석 조회(gql GetRemainSeats + seatMeta/seatStatus 비트맵) → available 좌석을
    등급/연석 조건으로 선택 → preselect → seats/select 확정.
  - 확정되면 가격·결제 단계는 브라우저 UI 로 넘겨 사람이 마무리한다.

seatStatus 비트맵: hex→2진수로 펼치면 seatMeta seats 배열과 같은 순서로 1:1 대응.
  비트 '1' = 예매가능, '0' = 점유/선택불가.
"""

import asyncio
import json
import os
import re
import time

from patchright.async_api import async_playwright
from browser_config import SESSION_PATH, get_launch_options

# 캡차 오디오 STT 자동 판독 로직 재사용
from captcha import solve_captcha
from telegram import send_telegram

# ────────────────────────────── 설정 ──────────────────────────────
# 예매 진입 URL (야놀자 → 인터파크 게이트로 이어짐). 실제 경기 페이지로 바꿔도 됨.
START_URL = "https://nol.yanolja.com/ticket/genre/sports/postseason"

# 좌석 페이지 URL 판별용 (여기 도달하면 좌석 자동선택 로직 시작)
SEAT_PAGE_RE = re.compile(r"tickets\.interpark\.com/onestop/seat")

# 원하는 좌석 조건
# scan 모드: 블록 코드 우선순위. 앞쪽부터 좌석 상태를 조회해 통로쪽(열 끝) 연석을 바로 선점한다.
# blockKey = "{playSeq}:{블록코드}" (예: "110" → "001:110"). "001:110" 처럼 전체를 적어도 된다.
# 휠체어석은 항상 제외. 테이블석처럼 인원이 고정된 상품(allocatedSeatCount>1)은 그 인원 묶음 전체를 잡는다.
PREFER_BLOCKS = [
    "318","319","320","317","316","321","322","323","324","325","326","327","328",
    "329","330","114","115","116","216","217","218","219","220","221","222","223",
    "224","117","118","119","225","226","120","121","331","332","333","315","314",
    "313","313","311","310","309","308","307","306","305","304","303","302","301",
    "421","420","419","418","417","416","415","414","413","412","411","410","409",
    "408","407","406","405","404","403","402","401",
]
# PREFER_BLOCKS = [
#     "117", "116", "118", "119", "120", "121", "122", "123", "124", "125", "126",
#     "518", "519", "520", "521", "522", "523", "524", "525", "526", "527", "528",
#     "529", "530", "531", "532", "533", "534",
# ]
TICKET_COUNT = 4               # 예매할 최대 매수 (연석)
MIN_TICKET_COUNT = 2           # 최소 매수. TICKET_COUNT 연석이 없으면 한 장씩 줄여 이 값까지 시도
PREFER_CONSECUTIVE = True      # True면 같은 열 연속 좌석(연석)만 선택(없으면 계속 대기)

# 좌석 선택 모드
#   "scan": 사람 클릭 없이 PREFER_BLOCKS 순서로 블록 좌석을 조회해 통로쪽 연석이 나오면 즉시 예매
#   "seed": 사람이 첫 좌석을 클릭하면 그 좌석 좌우로 연석 확장
SEAT_MODE = "scan"

# 좌석이 아직 안 열렸을 때 재조회 간격/최대 시도
POLL_INTERVAL_MS = 1000
MAX_POLL_ROUNDS = 2000

# 좌석 페이지 체류 시간(초). 서버 세션이 약 10분 뒤 끊기므로 여유를 두고 이 시간이 지나면
# 좌석 확인을 마치고(timeout) 호출자가 대기열부터 다시 진입한다.
SEAT_SESSION_SECONDS = 540

# 연석 확보가 이 횟수만큼 연속 실패하면 선점 좌석을 모두 풀고 첫 좌석부터 다시 고르게 한다.
FAIL_AFTER_ROUNDS = 10         # × POLL_INTERVAL_MS ≈ 3초

CONTEXT_OPTIONS = {
    "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "viewport": {"width": 1280, "height": 900},
    "locale": "ko-KR",
    "timezone_id": "Asia/Seoul",
}


async def wait_for_enter(prompt: str):
    """터미널 입력(Enter)을 비동기로 대기."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, input, prompt)


# ══════════════════════════════════════════════════════════════════════════
# 페이지 컨텍스트(브라우저)에서 실행할 JS.
# 기능별 조각을 js/ 디렉터리의 개별 .js 파일로 분리해 두고, 로드 순서대로 읽어
# 하나의 async 함수 본문으로 조합한다.
#   js/01_utils.js   : log/sleep/readCtx/randTrace/hexToBits 등 공용 유틸
#   js/02_http.js    : axios 인터셉터 재현(buildHeaders) + fetch 래퍼 j/gql
#   js/03_seats.js   : 잔여석 조회·블록 좌석 조회(seatMeta+seatStatus)·좌석 선택 로직
#   js/04_reserve.js : preselect → seats/select 선점/확정 (+ 실패 시 deselect)
#   js/05_ui_sync.js : 확보 좌석을 sessionStorage 컨텍스트에 주입 + 가격 단계로 이동
#   js/06_main_scan.js : 전 블록 폴링 → 통로쪽 연석 즉시 예매 (SEAT_MODE="scan")
#   js/06_main_seed.js : 사람이 첫 클릭한 좌석 기준 좌우 연석 확장 (SEAT_MODE="seed")
# ctx: {goodsCode, placeCode, playSeq, bizCode, sessionId, entMemberCode, token,
#       preferBlocks, deadlineMs, ticketCount, preferConsecutive, pollIntervalMs, maxPollRounds}
# ══════════════════════════════════════════════════════════════════════════

_JS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "js")

# 조합 순서(파일명). 순서가 곧 스코프 정의 순서이므로 유지해야 한다.
_JS_PARTS = [
    "01_utils.js",
    "02_http.js",
    "03_seats.js",
    "04_reserve.js",
    "05_ui_sync.js",
]
_MAIN_JS = {"seed": "06_main_seed.js", "scan": "06_main_scan.js"}


def _build_seat_automation_js():
    """js/ 의 기능별 조각들을 하나의 async 함수 본문으로 조합한다."""
    body = ""
    for name in _JS_PARTS + [_MAIN_JS[SEAT_MODE]]:
        with open(os.path.join(_JS_DIR, name), encoding="utf-8") as f:
            body += f.read()
    return (
        "async (ctx) => {\n"
        "  const {\n"
        "    goodsCode, placeCode, playSeq, bizCode, sessionId, entMemberCode, token,\n"
        "    ticketCount, preferConsecutive, pollIntervalMs, maxPollRounds,\n"
        "    seedSeatInfoId, seedBlockKey, failAfterRounds, preferBlocks, deadlineMs, minTicketCount,\n"
        "  } = ctx;\n"
        + body +
        "\n}\n"
    )


SEAT_AUTOMATION_JS = _build_seat_automation_js()

# 팝업/캡차 관련 셀렉터.
# CSS Modules 클래스 뒤에 붙는 해시(예: __jn1bV, __DC7Gz)는 빌드마다 바뀌므로
# 해시를 제외한 '접두사'만 부분 매칭(class*=)한다.
NOTICE_CONFIRM_BTN = 'button[class*="EntButton_primary"]'   # '확인하고 예매하기' (footer primary 버튼)
NOTICE_MODAL_TITLE = '[class*="ModalLayout_headerTitle"]'   # 모달 헤더 타이틀
CAPTCHA_INPUT = 'input[class*="ModalCaptchaText_captchaInput"]'
CAPTCHA_MODAL = '[class*="ModalCaptchaText_layerWrap"]'     # 캡차 모달 컨테이너
CAPTCHA_SUBMIT_BTN = 'button[class*="EntButton_primary"]'  # '입력완료' 버튼(캡차 모달 내 primary)


async def attach_captcha_hook(page):
    """captcha/image 요청만 라우팅으로 가로채 EncRnd 를 확보한다.

    page.on("response") 는 모든 응답마다 콜백이 돌지만, page.route 는 패턴에 매칭되는
    요청에만 콜백이 붙어 효율적이다. route.fetch() 로 실제 응답을 받아 EncRnd 를 읽고,
    route.fulfill() 로 그대로 페이지에 돌려주므로 프론트 동작에는 영향이 없다.
    반환한 dict 의 'encRnd' 에 최신값이 담긴다.
    """
    store = {"encRnd": None}

    async def _route(route):
        try:
            response = await route.fetch()
            try:
                body = await response.json()
                enc = body.get("EncRnd")
                if enc:
                    store["encRnd"] = enc
                    print("🪝 captcha/image 후킹 — EncRnd 확보")
            except Exception:
                pass
            await route.fulfill(response=response)
        except Exception:
            # 가로채기 실패 시 원본 요청을 그대로 흘려보내 페이지가 깨지지 않게 한다.
            await route.continue_()

    await page.route("**/onestop/api/captcha/image", _route)
    return store


async def _dismiss_notice_popup(page):
    """'예매 안내' 팝업이 떠 있으면 '확인하고 예매하기'를 눌러 닫는다.

    캡차 모달에도 동일한 primary 버튼(입력완료)이 있으므로,
    '예매 안내' 헤더가 있는 경우에만 클릭한다.
    """
    try:
        # 예매 안내 모달 헤더가 나타나길 잠깐 기다린다(없으면 그냥 지나감).
        title = page.locator(NOTICE_MODAL_TITLE, has_text="예매 안내")
        try:
            await title.wait_for(state="visible", timeout=0)
        except Exception:
            print("ℹ️ '예매 안내' 팝업이 감지되지 않음 — 건너뜀")
            return

        # 안내 모달 안의 '확인하고 예매하기' 버튼 클릭
        btn = page.get_by_role("button", name="확인하고 예매하기")
        if await btn.count() == 0:
            btn = page.locator(NOTICE_CONFIRM_BTN).first
        await btn.click(timeout=5000)
        print("✅ '예매 안내' 팝업 닫음 (확인하고 예매하기 클릭)")
        await page.wait_for_timeout(500)
    except Exception as e:
        print(f"⚠️ 예매 안내 팝업 처리 중 예외(무시하고 진행): {e}")


async def _captcha_modal_closed(page, captcha_input):
    """캡차 모달(또는 입력창)이 사라졌는지 확인."""
    try:
        if await captcha_input.count() == 0:
            return True
        return not await captcha_input.is_visible()
    except Exception:
        return True


async def _wait_for_captcha(page, enc_store=None, max_attempts=5):
    """캡차를 오디오 STT 로 자동 판독해 입력하고 '입력완료'를 누른다.

    흐름: 입력창 감지 → (후킹으로 받은 EncRnd 로) 오디오 STT → 입력 → 입력완료 클릭
          → 모달이 닫히면 성공. 안 닫히면(=오답) 새 캡차로 max_attempts 까지 재시도.
    자동 판독이 계속 실패하면 마지막엔 사람이 직접 풀도록 대기로 폴백한다.
    캡차가 애초에 없으면 곧바로 통과한다.
    """
    captcha_input = page.locator(CAPTCHA_INPUT)
    try:
        await captcha_input.wait_for(state="visible", timeout=2000)
    except Exception:
        print("ℹ️ 캡차 입력창이 감지되지 않음 — 캡차 없이 진행")
        return

    for attempt in range(1, max_attempts + 1):
        # 모달이 이미 닫혔으면 성공으로 간주
        if await _captcha_modal_closed(page, captcha_input):
            print("✅ 캡차 모달이 닫힘 — 좌석 선택 단계로 진행")
            return

        enc_rnd = enc_store.get("encRnd") if enc_store else None
        print(f"🔊 캡차 자동 판독 시도 {attempt}/{max_attempts} "
              f"(EncRnd={'후킹값' if enc_rnd else '직접요청'})")
        res = await solve_captcha(page.request, name=f"auto_{attempt}", enc_rnd=enc_rnd)

        if not res.get("ok"):
            print(f"   ⚠️ STT 실패: {res.get('error')} — 새 캡차로 재시도")
            # 새 문자 불러오기(있으면) 후 다음 루프에서 다시 후킹된 EncRnd 사용
            await _refresh_captcha(page)
            if enc_store:
                enc_store["encRnd"] = None  # 다음 후킹값을 기다리도록 초기화
            await page.wait_for_timeout(800)
            continue

        text = res["text"]
        print(f"   📝 인식 결과: {text} — 입력창에 채우고 '입력완료' 클릭")
        await captcha_input.click(timeout=2000)
        await captcha_input.fill("")
        await captcha_input.fill(text)
        await page.wait_for_timeout(200)

        # '입력완료' 버튼 클릭 (활성화됐을 때)
        submitted = await _click_captcha_submit(page)
        if not submitted:
            print("   ⚠️ '입력완료' 버튼을 누르지 못함 — 재시도")
            await _refresh_captcha(page)
            if enc_store:
                enc_store["encRnd"] = None
            await page.wait_for_timeout(800)
            continue

        # 모달이 닫히는지 잠깐 확인 (닫히면 정답)
        await page.wait_for_timeout(1200)
        if await _captcha_modal_closed(page, captcha_input):
            print("✅ 캡차 통과 — 좌석 선택 단계로 진행")
            return
        print("   ❌ 오답으로 보임(모달 유지) — 새 캡차로 재시도")
        await _refresh_captcha(page)
        if enc_store:
            enc_store["encRnd"] = None
        await page.wait_for_timeout(800)

    # 자동 판독 실패 → 사람이 직접 풀도록 폴백
    print("⚠️ 자동 캡차 판독 실패. 브라우저에서 직접 입력해주세요. (모달이 닫히면 계속 진행)")
    try:
        await captcha_input.focus()
    except Exception:
        pass
    try:
        await page.locator(CAPTCHA_MODAL).wait_for(state="hidden", timeout=3_600_000)
    except Exception:
        try:
            await captcha_input.wait_for(state="detached", timeout=5000)
        except Exception:
            pass
    print("✅ 캡차 종료 — 좌석 선택 단계로 진행")


async def _refresh_captcha(page):
    """'새 문자 불러오기' 버튼이 있으면 눌러 캡차를 갱신한다(없으면 무시)."""
    try:
        btn = page.locator('button[class*="ModalCaptchaText_buttonRefresh"]')
        if await btn.count() > 0:
            await btn.first.click(timeout=2000)
    except Exception:
        pass


async def _click_captcha_submit(page):
    """캡차 '입력완료' 버튼을 클릭. 성공 여부 반환."""
    try:
        btn = page.get_by_role("button", name="입력완료")
        if await btn.count() == 0:
            btn = page.locator(CAPTCHA_SUBMIT_BTN).last
        # disabled 면 잠깐 기다린다(입력값 반영 후 활성화)
        for _ in range(10):
            if await btn.is_enabled():
                break
            await page.wait_for_timeout(150)
        await btn.click(timeout=2000)
        return True
    except Exception as e:
        print(f"   ⚠️ 입력완료 클릭 예외: {e}")
        return False


async def _extract_context(page):
    """좌석 페이지에서 예매에 필요한 컨텍스트(token/sessionId 등)를 추출한다.

    진입 HTML 안 window 데이터에 initData.token / sessionId / entMemberCode 가 있고,
    좌석 페이지 URL 쿼리에 goodsCode/placeCode/playSeq/bizCode 가 들어있다.
    페이지 내 JS 전역 어디에 실렸는지는 배포마다 다를 수 있어, 여러 경로를 폴백으로 훑는다.
    """
    return await page.evaluate(r"""
    () => {
      const out = {};
      // URL 쿼리에서 기본 파라미터
      const url = new URL(location.href);
      const qs = url.searchParams;

      // window 전역/__NEXT_DATA__ 등에서 initData 흔적 탐색
      const scan = (obj, depth = 0) => {
        if (!obj || depth > 6 || typeof obj !== "object") return;
        for (const k of Object.keys(obj)) {
          let v;
          try { v = obj[k]; } catch { continue; }
          if (v && typeof v === "object") {
            if (v.token && v.sessionId) {
              out.token = out.token || v.token;
              out.sessionId = out.sessionId || v.sessionId;
              out.entMemberCode = out.entMemberCode || v.entMemberCode;
              out.goodsCode = out.goodsCode || v.goodsCode;
              out.placeCode = out.placeCode || v.placeCode;
              out.playSeq = out.playSeq || v.playSeq;
              out.bizCode = out.bizCode || v.bizCode;
            }
            scan(v, depth + 1);
          }
        }
      };
      try { scan(window.__NEXT_DATA__); } catch {}
      // 페이지 HTML 원문에서 정규식으로 백업 추출
      const html = document.documentElement.innerHTML;
      const pick = (re) => { const m = html.match(re); return m ? m[1] : null; };
      out.token = out.token || pick(/"token":"([^"]+)"/);
      out.sessionId = out.sessionId || pick(/"sessionId":"([^"]+)"/);
      out.entMemberCode = out.entMemberCode || pick(/"entMemberCode":"([^"]+)"/);
      out.goodsCode = out.goodsCode || pick(/"goodsCode":"(\d+)"/) || qs.get("goodsCode");
      out.placeCode = out.placeCode || pick(/"placeCode":"(\d+)"/) || qs.get("placeCode");
      out.playSeq = out.playSeq || pick(/"playSeq":"(\d+)"/) || qs.get("playSeq") || "001";
      out.bizCode = out.bizCode || pick(/"bizCode":"(\d+)"/) || qs.get("bizCode");
      // sessionId 안에 goodsCode 가 prefix 로 들어있는 경우 보정
      if (!out.goodsCode && out.sessionId) {
        const m = out.sessionId.match(/^(\d+)_/); if (m) out.goodsCode = m[1];
      }
      return out;
    }
    """)


def attach_console_forwarding(page):
    """브라우저 콘솔([auto] …) 로그를 파이썬 터미널로 그대로 포워딩한다."""
    def _on_console(msg):
        try:
            txt = msg.text
        except Exception:
            txt = str(msg)
        if "[auto]" in txt:
            print("🟦", txt)
    page.on("console", _on_console)
    page.on("pageerror", lambda e: print("🟥 PAGE ERROR:", e))


async def wait_for_seat_page(page, timeout_s=3600):
    """현재 페이지가 좌석 페이지(/onestop/seat)로 진입할 때까지 대기."""
    print("⏳ 좌석 페이지 진입 대기 중…")
    for _ in range(timeout_s * 10):  # 0.1초 간격
        if SEAT_PAGE_RE.search(page.url):
            print(f"✅ 좌석 페이지 감지: {page.url}")
            return True
        await page.wait_for_timeout(100)
    print("❌ 좌석 페이지에 진입하지 못했습니다.")
    return False


async def wait_for_first_click(page, timeout_s=3600):
    """사용자가 좌석을 처음 클릭할 때 프론트가 보내는 PreselectSeat 요청을 가로채 seed 를 얻는다."""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def _on_request(req):
        if fut.done() or "/onestop/gql" not in req.url or req.method != "POST":
            return
        try:
            body = json.loads(req.post_data or "{}")
        except Exception:
            return
        if body.get("operationName") == "PreselectSeat":
            cmd = (body.get("variables") or {}).get("command") or {}
            if cmd.get("seatInfoId"):
                fut.set_result(cmd)

    page.on("request", _on_request)
    try:
        return await asyncio.wait_for(fut, timeout_s)
    except asyncio.TimeoutError:
        return None
    finally:
        page.remove_listener("request", _on_request)


async def run_booking(page):
    """좌석 페이지에서 팝업/캡차 처리 후 좌석 자동선택을 수행한다.

    호출 전제: page 가 이미 좌석 페이지(/onestop/seat)에 있거나 곧 진입한다.
    반환: 좌석 자동선택 결과 dict. 판정 불가 시 requires_review=True.
    """
    # captcha/image 응답 후킹을 먼저 등록(캡차가 뜨기 전에 라우트가 붙어 있어야 EncRnd 확보).
    enc_store = await attach_captcha_hook(page)

    # 좌석 페이지 진입 대기
    if not await wait_for_seat_page(page):
        return {"ok": False, "reason": "no-seat-page"}

    # 1) '예매 안내' 팝업이 뜨면 '확인하고 예매하기' 눌러 닫는다.
    await _dismiss_notice_popup(page)

    # 2) 캡차를 오디오 STT 로 자동 판독해 입력하고 '입력완료'까지 누른다.
    #    자동 판독이 실패하면 사람이 직접 풀도록 폴백한다.
    await _wait_for_captcha(page, enc_store=enc_store)

    ctx = await _extract_context(page)
    missing = [k for k in ("token", "sessionId", "goodsCode", "placeCode", "playSeq", "bizCode")
               if not ctx.get(k)]
    print("🔑 추출된 컨텍스트:", json.dumps(
        {k: (v[:12] + "…" if isinstance(v, str) and len(v) > 12 else v) for k, v in ctx.items()},
        ensure_ascii=False))
    if missing:
        print(f"⚠️ 누락된 값: {missing} — 캡차 통과 후 다시 진입했는지 확인하세요.")
        print("   그래도 자동선택을 시도합니다(일부는 URL 쿼리로 보정됨).")

    result = None
    deadline_ms = int(time.time() * 1000) + SEAT_SESSION_SECONDS * 1000   # 이 시각이 지나면 scan 이 timeout 반환
    while True:
        if SEAT_MODE == "scan":
            seed = {"seatInfoId": "", "blockKey": ""}
            print("\n🔍 취소표 연석 스캔 시작…")
        else:
            # 첫 좌석은 사람이 직접 클릭한다. 클릭 시 나가는 PreselectSeat 요청에서 seed 좌석을 얻는다.
            print("\n👆 원하는 좌석을 직접 한 번 클릭하세요. 그 좌석 기준 좌우 연석으로 자동 선택합니다.")
            seed = await wait_for_first_click(page)
            if not seed:
                return {"ok": False, "reason": "no-first-click"}
            print(f"🎯 첫 클릭 감지: {seed['seatInfoId']} (블록 {seed['blockKey']})")

        ctx.update({
            "ticketCount": TICKET_COUNT,
            "minTicketCount": MIN_TICKET_COUNT,
            "preferConsecutive": PREFER_CONSECUTIVE,
            "pollIntervalMs": POLL_INTERVAL_MS,
            "maxPollRounds": MAX_POLL_ROUNDS,
            "failAfterRounds": FAIL_AFTER_ROUNDS,
            "preferBlocks": PREFER_BLOCKS,
            "deadlineMs": deadline_ms,
            "seedSeatInfoId": seed["seatInfoId"],
            "seedBlockKey": seed["blockKey"],
        })

        try:
            result = await page.evaluate(SEAT_AUTOMATION_JS, ctx)
        except Exception as e:
            # 요청이 서버에 반영된 뒤 응답만 유실됐을 수도 있으므로 실패/성공을 추정하지 않는다.
            print(f"⚠️ 좌석 처리 결과 확인 불가: {type(e).__name__}")
            result = {"ok": False, "reason": "result-unknown", "requires_review": True}
            break

        if result and result.get("reason") == "wheelchair":
            print("\n♿ 휠체어석은 제외 대상입니다 → 선택 좌석을 풀었습니다. 다른 좌석을 다시 클릭하세요.")
            continue
        if result and result.get("reason") == "no-adjacent":
            print(f"\n⚠️ 연석 {TICKET_COUNT}석 확보 실패 → 선택 좌석을 모두 풀었습니다. 다른 좌석을 다시 클릭하세요.")
            print("   (화면에 이전 선택이 남아 있으면 해당 좌석을 한 번 눌러 해제한 뒤 다시 클릭하세요.)")
            continue
        break

    if not isinstance(result, dict):
        result = {"ok": False, "reason": "invalid-result", "requires_review": True}

    if result.get("ok"):
        print("\n🎉 연석 확보 성공!")
        for s in result["seats"]:
            print(f"   - {s['rowNo']} {s['seatNo']}번 (등급 {s['seatGrade']}, {s['price']}원) "
                  f"{s['seatInfoId']}")
        print("\n👉 화면이 가격/결제 단계로 이동했습니다. 브라우저에서 마무리하세요.")
        lines = [f"{x['rowNo']} {x['seatNo']}번 ({x['price']}원)" for x in result["seats"]]
        result["notification_sent"] = await send_telegram("🎉 좌석 선점 성공! 지금 결제하세요(선점 시간 제한).\n" + "\n".join(lines))
    elif result.get("requires_review"):
        print("\n⚠️ 좌석 확보 여부를 확인할 수 없습니다. 브라우저에서 직접 확인하세요.")
    else:
        print(f"\n❌ 좌석 확보 실패: {result.get('reason')}")
    if result.get("notification_sent") is False:
        print("🚨 알림 전달 실패: 예약을 다시 시도하지 말고 현재 브라우저 상태를 확인하세요.")
    return result


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(**get_launch_options())

        options = dict(CONTEXT_OPTIONS)
        if os.path.exists(SESSION_PATH):
            options["storage_state"] = SESSION_PATH
            print(f"📂 세션 파일 사용(로그인 유효 여부는 별도 확인): {SESSION_PATH}")
        else:
            print(f"ℹ️ 세션 파일 없음 — 새 세션 시작: {SESSION_PATH}")

        context = await browser.new_context(**options)
        page = await context.new_page()
        attach_console_forwarding(page)

        print(f"🌐 접속: {START_URL}")
        await page.goto(START_URL, wait_until="domcontentloaded", timeout=30_000)

        print("\n" + "=" * 66)
        print("👉 브라우저에서 예매를 진행하세요:")
        print("   경기 선택 → 예매하기 → (대기열 통과) → 좌석 페이지 진입")
        print("   좌석 페이지(/onestop/seat)에 들어오고 '캡차까지 통과'하면")
        print("   이 스크립트가 자동으로 좌석을 잡습니다.")
        print("=" * 66 + "\n")

        await run_booking(page)

        # 결제를 사람이 마치도록 대기. 엔터를 누르면 세션 저장 후 종료한다.
        await wait_for_enter("\n⏳ 결제/마무리 후 이 터미널에서 Enter 를 누르면 종료합니다... ")

        await context.storage_state(path=SESSION_PATH)
        print(f"✅ 세션 저장 완료: {SESSION_PATH}")
        await context.close()
        await browser.close()
        print("🔚 브라우저 종료")


if __name__ == "__main__":
    asyncio.run(main())
