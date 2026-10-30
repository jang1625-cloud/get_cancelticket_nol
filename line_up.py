"""대기열 진입 체인 (Playwright page.request 로 호출).

체인:
  member-info → secure-url → (redirectUrl 의 key 추출) → line-up → rank → 응답 출력

특징:
  - page.evaluate 로 JS 를 주입하는 대신, Playwright 의 APIRequestContext(page.request)
    로 파이썬에서 직접 HTTP 호출한다. 브라우저 컨텍스트의 쿠키/세션을 공유하므로
    로그인 상태가 그대로 실린다.
  - 단독 실행도 가능하고(main), run_line_up(page, ...) 로 다른 스크립트에서 import 해
    호출할 수도 있다.
  - 각 단계는 개별 함수로 분리:
      fetch_member_info → fetch_goods_info → measure_clock_skew → wait_until_open
      → fetch_secure_url → fetch_line_up → poll_rank
    run_line_up 은 이들을 순서대로 엮는 오케스트레이터다.
  - goods-info 의 ticketOpenDate 로 오픈 시각까지 대기하되, getServerTime 으로 측정한
    서버-로컬 시간차(skew)를 반영해 '서버 기준' 오픈 순간에 맞춰 secure-url 을 시작한다.

주의:
  - 원본 요청의 credentials 는 제각각(member-info/line-up=include, secure-url/rank=omit).
    page.request 는 컨텍스트 쿠키를 기본으로 싣는다. omit 이어야 하는 요청은 서버가
    쿠키를 무시하는 것으로 보이지만, 문제가 되면 별도 request context 로 분리하면 된다.
"""

import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, parse_qs

from patchright.async_api import async_playwright
from browser_config import SESSION_PATH, get_launch_options
from telegram import send_telegram
from terminal_log import terminal_log

KST = timezone(timedelta(hours=9))  # ticketOpenDate 는 한국시간(KST) 기준

# booking.py 의 좌석 자동선택 로직을 그대로 재사용한다.
from booking import (
    attach_captcha_hook,
    attach_console_forwarding,
    run_booking,
    wait_for_enter,
)

# https://tickets.interpark.com/gates/ticket/26014409?bc=61776&pc=26001228&ps=&pass_code=
# https://tickets.interpark.com/gates/ticket/26014411?bc=61776&pc=26001228&ps=&pass_code=

# test 
# https://tickets.interpark.com/gates/ticket/26012741?bc=61776&pc=26000895&ps=&pass_code=
# 대상 정보
GOODS_CODE = "26014439"
PLACE_CODE = "26000116"
BIZ_CODE = "61776"
CHANNEL_CODE = "pm"
LANG = "ko"
FROM = "NOL"

# 401일 때만 목록에서 클릭할 경기. GOODS_CODE를 바꾸면 날짜·시간도 함께 맞춘다.
RECOVERY_LIST_URL = "https://nol.yanolja.com/ticket/genre/sports/postseason"
RECOVERY_GAME_DATETIME = (10, 20, 18, 30)  # 월, 일, 시, 분

CONTEXT_OPTIONS = {
    "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "viewport": {"width": 1480, "height": 1200},
    "locale": "ko-KR",
    "timezone_id": "Asia/Seoul",
}


def _extract_key(redirect_url: str):
    """redirectUrl 쿼리에서 key 값을 뽑아 URL 디코드된 상태로 반환."""
    try:
        qs = parse_qs(urlparse(redirect_url).query)
        vals = qs.get("key")
        return vals[0] if vals else None
    except Exception:
        return None


# ── 공용 엔드포인트/헤더 ────────────────────────────────────────────────────
TICKETS_ORIGIN = "https://tickets.interpark.com"
WAITING_API = "https://ent-waiting-api.interpark.com/waiting/api"
MEMBER_INFO_URL = f"{TICKETS_ORIGIN}/api/ticket/v2/reserve-gate/member-info"
GOODS_INFO_URL = f"{TICKETS_ORIGIN}/api/ticket/v2/reserve-gate/goods-info"
SERVER_TIME_URL = "https://api-ticketfront.interpark.com/v1/getServerTime"

# 대기열 API(secure-url/line-up/rank) 공통 헤더. referer 는 tickets 루트.
_WAITING_HEADERS = {
    "accept": "*/*",
    "cache-control": "no-cache",
    "pragma": "no-cache",
    "referer": f"{TICKETS_ORIGIN}/",
}


class LineUpError(Exception):
    """대기열 진입 단계 실패. step/status 를 함께 전달한다."""

    def __init__(self, step, status=None, body=None, extra=None):
        self.step = step
        self.status = status
        self.body = body
        self.extra = extra or {}
        super().__init__(f"{step} 실패 (status={status})")


async def fetch_member_info(req, goods_code, channel_code):
    """1) member-info : 예매 서명(signature/secureData) 획득.
    반환: member-info 응답 dict (signature, secureData, name, ... 포함)."""
    print("1) member-info 호출")
    res = await req.get(
        MEMBER_INFO_URL,
        params={"goodsCode": goods_code, "channelCode": channel_code},
        headers={
            "accept": "*/*",
            "cache-control": "default",
            "pragma": "no-cache",
            "referer": f"{TICKETS_ORIGIN}/gates/ticket/{goods_code}",
        },
    )
    if not res.ok:
        raise LineUpError("member-info", res.status, (await res.text())[:200])
    body = await res.json()
    if not body.get("signature"):
        raise LineUpError("member-info", res.status, body)
    print(f"   OK - name={body.get('name')} signature={body.get('signature')}")
    return body


async def fetch_goods_info(req, goods_code, biz_code, place_code, lang):
    """상품 정보 조회 → ticketOpenDate(오픈시각) 등 포함 dict 반환."""
    print("1.5) goods-info 호출 (오픈시각 확인)")
    res = await req.get(
        GOODS_INFO_URL,
        params={
            "bizCode": biz_code,
            "goodsCode": goods_code,
            "lang": lang,
            "passCode": "",
            "placeCode": place_code,
        },
        headers={
            "accept": "*/*",
            "cache-control": "no-cache",
            "pragma": "no-cache",
            "referer": f"{TICKETS_ORIGIN}/gates/ticket/{goods_code}",
        },
    )
    if not res.ok:
        raise LineUpError("goods-info", res.status, (await res.text())[:200])
    body = await res.json()
    print(f"   OK - {body.get('goodsName')} | ticketOpenDate={body.get('ticketOpenDate')}")
    return body


def _parse_open_dt(ticket_open_date):
    """ticketOpenDate('YYYYMMDDHHMMSS', KST) → aware datetime. 파싱 실패 시 None."""
    if not ticket_open_date or len(str(ticket_open_date)) < 14:
        return None
    try:
        return datetime.strptime(str(ticket_open_date)[:14], "%Y%m%d%H%M%S").replace(tzinfo=KST)
    except Exception:
        return None


async def measure_clock_skew(req, samples=5):
    """getServerTime 으로 (서버시각 - 로컬시각) 오차(ms)를 측정해 반환.

    각 샘플마다 요청 직전(t0)·직후(t1) 로컬시각을 재고, 서버시각은 t0~t1 중간(RTT/2 보정)
    시점의 값으로 본다. 그중 RTT 가 가장 작은(가장 신뢰도 높은) 샘플의 skew 를 채택한다.
      skew = server_ms - local_mid   (양수면 서버가 로컬보다 미래)
    측정 실패 시 0.0 반환(=로컬 시계 그대로 사용).
    """
    best = None  # (rtt, skew)
    for i in range(samples):
        try:
            nc = int(time.time() * 1000)
            t0 = time.time() * 1000
            res = await req.get(
                f"{SERVER_TIME_URL}?type=1&nc={nc}",
                headers={"accept": "*/*", "cache-control": "no-cache",
                         "pragma": "no-cache", "referer": f"{TICKETS_ORIGIN}/"},
            )
            t1 = time.time() * 1000
            if not res.ok:
                continue
            text = (await res.text()).strip()
            m = re.search(r"-?\d+", text)
            if not m:
                continue
            server_ms = int(m.group())
            rtt = t1 - t0
            local_mid = (t0 + t1) / 2.0
            skew = server_ms - local_mid
            if best is None or rtt < best[0]:
                best = (rtt, skew)
        except Exception:
            continue
        await asyncio.sleep(0.1)

    if best is None:
        print("   ⚠️ getServerTime 측정 실패 — 로컬 시계 사용(skew=0)")
        return 0.0
    rtt, skew = best
    print(f"⏱️  서버 시간차 측정: skew={skew:+.0f}ms (best RTT={rtt:.0f}ms) "
          f"→ 서버가 로컬보다 {'미래' if skew >= 0 else '과거'}")
    return skew


def _server_now_ms(skew_ms):
    """현재 서버 추정 시각(epoch millis) = 로컬시각 + skew."""
    return time.time() * 1000 + skew_ms


async def wait_until_open(ticket_open_date, skew_ms=0.0, lead_ms=300):
    """서버 기준 시각으로 ticketOpenDate 까지 대기한다.

    로컬 시계가 아니라 (로컬 + skew_ms) 로 보정한 '서버 추정 시각'을 오픈시각과 비교하므로,
    내 컴퓨터 시계가 어긋나 있어도 실제 오픈 순간에 맞출 수 있다.
    오픈 lead_ms 직전에 깨어나 곧바로 다음 단계로 넘어간다.
    """
    open_dt = _parse_open_dt(ticket_open_date)
    if open_dt is None:
        print("   ⚠️ ticketOpenDate 파싱 실패 — 대기 없이 진행")
        return

    open_ms = open_dt.timestamp() * 1000
    target_ms = open_ms - lead_ms  # 살짝 먼저 준비

    remain = (target_ms - _server_now_ms(skew_ms)) / 1000.0
    if remain <= 0:
        print(f"   ✅ 이미 오픈 시각 지남(open={open_dt:%Y-%m-%d %H:%M:%S} KST) — 즉시 진행")
        return

    print(f"⏰ 오픈 대기(서버기준): {open_dt:%Y-%m-%d %H:%M:%S} KST 까지 "
          f"약 {remain:.1f}초 남음 (skew={skew_ms:+.0f}ms 반영)")
    while True:
        remain = (target_ms - _server_now_ms(skew_ms)) / 1000.0
        if remain <= 0:
            break
        # 남은 시간에 따라 sleep 간격 조절 (막판엔 촘촘히)
        if remain > 60:
            step = 30
        elif remain > 5:
            step = 1
        elif remain > 1:
            step = 0.1
        else:
            step = 0.01  # 마지막 1초는 10ms 단위로 정밀 대기
        await asyncio.sleep(min(step, remain))
    print(f"🚀 오픈!(서버기준 {open_dt:%H:%M:%S}) — secure-url 시작")


# 일제발사(salvo) 타이밍: 오픈시각 기준 상대 오프셋(ms). -50 ~ +20 을 10ms 간격으로 8발.
_SALVO_OFFSETS_MS = [-150, -100, -70, -50, -25, -10, 0]


# 오픈 직전 연결 예열: 일제발사 시 각 발이 새 TCP/TLS 핸드셰이크를 하지 않도록,
# 오픈 WARM_LEAD_S 초 전에 WAITING_API 호스트로 발사 수만큼 동시에 가벼운 요청을 보내 연결을 미리 만든다.
WARM_LEAD_S = 3.0


async def _warm_one(req):
    try:
        res = await req.get(f"{WAITING_API}/rank", params={"waitingId": "warmup"}, headers=_WAITING_HEADERS)
        await res.body()      # 본문까지 받아야 연결이 풀로 반환된다
    except Exception:
        pass                  # 상태/에러는 무관: 연결 수립이 목적


async def warm_connections(req, n=None):
    n = n or len(_SALVO_OFFSETS_MS) + 1
    t0 = time.time()
    await asyncio.gather(*(_warm_one(req) for _ in range(n)))
    print(f"   🔥 연결 예열 완료 ({n}개 동시, {(time.time() - t0) * 1000:.0f}ms)")


async def _warm_before_open(req, ticket_open_date, skew_ms):
    """오픈 WARM_LEAD_S 초 전까지 기다렸다가 연결을 예열한다(이미 가까우면 즉시)."""
    open_dt = _parse_open_dt(ticket_open_date)
    if open_dt is None:
        return
    remain = (open_dt.timestamp() * 1000 - _server_now_ms(skew_ms)) / 1000.0
    if remain <= 0.3:
        return   # 이미 오픈/임박: 예열이 오히려 발사를 늦춘다
    await asyncio.sleep(max(0.0, remain - WARM_LEAD_S))
    await warm_connections(req)


def _is_not_open_yet(status, body_text):
    """응답이 '아직 오픈 전(UnableReservationTime)'인지 판정."""
    return status == 403 and "UnableReservationTime" in body_text


async def _post_secure_url_at(req, headers, payload, fire_at_local_ms, offset_ms):
    """fire_at_local_ms(로컬 epoch ms) 시점까지 대기 후 secure-url 을 1회 POST.

    반환: (ok, status, body_or_text, offset_ms)
      - ok=True 면 body(json dict), 아니면 텍스트/에러 문자열.
    """
    delay = (fire_at_local_ms - time.time() * 1000) / 1000.0
    if delay > 0:
        await asyncio.sleep(delay)
    try:
        res = await req.post(f"{WAITING_API}/secure-url", headers=headers, data=payload)
        text = await res.text()   # 본문은 한 번만 읽는다(핫패스: 출력/중복 파싱 금지)
        if res.ok:
            try:
                return (True, res.status, json.loads(text), offset_ms)
            except Exception:
                return (True, res.status, {}, offset_ms)
        return (False, res.status, text[:200], offset_ms)
    except Exception as e:
        return (False, None, f"예외: {e}", offset_ms)


async def fetch_secure_url(req, member_info, biz_code, lang, from_,
                           ticket_open_date=None, skew_ms=0.0, interval_s=1):
    """2) secure-url : signature/secureData 로 redirectUrl 발급 → key 추출.

    오픈 정각의 RTT 지연으로 미세하게 늦는 걸 줄이기 위해, 오픈시각 기준
    -50~+20ms 를 10ms 간격으로 어긋나게 8발을 '동시에' 스케줄해 쏘고,
    그중 가장 먼저 성공한 응답을 채택한다(나머지는 취소).
    아직 오픈 전(403 UnableReservationTime)이면 다음 라운드로 재시도한다.
    ticket_open_date 가 없으면 기존처럼 단발 재시도로 동작한다.
    반환: (redirect_url, key)."""
    print("2) secure-url 호출 (일제발사)")
    payload = {
        "signature": member_info["signature"],
        "secureData": member_info["secureData"],
        "lang": lang,
        "passCode": "",
        "from": from_,
        "bizCode": biz_code,
        "partnerMemBizCode": biz_code,
        "bpk": "cttftEuefGjbkQFc4JkU1A==",
        "partnerMemId": "66nPrfLLJnQNBP6TPTg0GA==",
        "preSales": "N",
    }
    headers = {**_WAITING_HEADERS, "accept": "application/json", "content-type": "application/json"}

    open_dt = _parse_open_dt(ticket_open_date) if ticket_open_date else None

    while True:
        if open_dt is not None:
            # 서버기준 오픈시각(ms)을 로컬 시계로 환산: local = server - skew.
            open_server_ms = open_dt.timestamp() * 1000
            open_local_ms = open_server_ms - skew_ms
            # 각 오프셋 시각에 발사하도록 8발을 동시에 스케줄
            tasks = [
                asyncio.create_task(
                    _post_secure_url_at(req, headers, payload, open_local_ms + off, off))
                for off in _SALVO_OFFSETS_MS
            ]
            success = None
            not_open = False
            other_err = None
            # 먼저 끝나는 것부터 확인해, 성공이 나오면 즉시 채택하고 나머지 취소
            for fut in asyncio.as_completed(tasks):
                ok, status, data, off = await fut
                if ok:
                    success = (data, off)
                    break
                if _is_not_open_yet(status, str(data)):
                    not_open = True
                elif status is not None:
                    other_err = (status, data)
            for t in tasks:
                t.cancel()

            if success:
                body, off = success
                print(f"   ✅ 일제발사 성공 (오프셋 {off:+d}ms) {body}")
                break
            if other_err and not not_open:
                # 오픈 전이 아닌 실제 오류만 있었으면 실패 처리
                raise LineUpError("secure-url", other_err[0], other_err[1])
            print("   ⏳ 아직 오픈 전(UnableReservationTime) — 재발사")
            await asyncio.sleep(interval_s)
            continue

        # ticket_open_date 미지정: 기존 단발 재시도
        res = await req.post(f"{WAITING_API}/secure-url", headers=headers, data=payload)
        if res.ok:
            body = await res.json()
            break
        body_text = (await res.text())[:200]
        if _is_not_open_yet(res.status, body_text):
            print("   ⏳ 아직 오픈 전(UnableReservationTime) — 재시도")
            await asyncio.sleep(interval_s)
            continue
        raise LineUpError("secure-url", res.status, body_text)

    redirect_url = body.get("redirectUrl")
    key = _extract_key(redirect_url) if redirect_url else None
    if not key:
        raise LineUpError("extract-key", extra={"redirectUrl": redirect_url})
    print("   OK - key 추출 완료")
    return redirect_url, key


async def fetch_line_up(req, key):
    """3) line-up : key 로 대기열 등록 → waitingId 발급.
    반환: line-up 응답 dict (waitingId, userSeq, ... 포함)."""
    print("3) line-up 호출")
    res = await req.post(
        f"{WAITING_API}/line-up",
        headers={**_WAITING_HEADERS, "content-type": "application/json"},
        data={"key": key},
    )
    if not res.ok:
        raise LineUpError("line-up", res.status, (await res.text())[:200])
    body = await res.json()
    if not body.get("waitingId"):
        raise LineUpError("line-up", res.status, body)
    print(f"   OK - waitingId={body.get('waitingId')} userSeq={body.get('userSeq')}")
    return body


RANK_BURST_SIZE = 1       # myRank 발급 전 동시 요청 수
RANK_BURST_GAP_S = 1    # 동시 요청 묶음 사이 간격


async def _fetch_rank_once(req, waiting_id):
    """rank 1회 호출. 성공하면 응답 dict, 실패(비-2xx/예외)면 None."""
    try:
        res = await req.get(
            f"{WAITING_API}/rank",
            params={"waitingId": waiting_id},
            headers=_WAITING_HEADERS,
        )
        if not res.ok:
            print(f"   ⚠️ rank status={res.status} body={str(await res.text())[:200]}")
            return None
        return await res.json()
    except Exception as e:
        print(f"   ⚠️ rank 예외: {e}")
        return None


async def poll_rank(req, waiting_id, interval_s=1):
    """4) rank : 응답에 oneStopUrl 키가 생길 때까지 폴링.

    - 첫 시도이거나 myRank 가 아직 None 이면 RANK_BURST_SIZE 개를 동시에 보낸다.
      하나라도 myRank(또는 oneStopUrl)가 발급되면 그 시점부터 interval_s 간격 단건 폴링으로 전환,
      아니면 RANK_BURST_GAP_S 후 다시 동시 요청한다.
    반환: (rank 응답 dict, one_stop_url)."""
    print(f"4) rank 폴링 시작 (myRank 발급 전 {RANK_BURST_SIZE}건 동시 → 발급 후 {interval_s}초 간격)")

    def _report(body):
        print(f"   myRank={body.get('myRank')} totalRank={body.get('totalRank')} "
              f"bookingRate={body.get('bookingRate')} oneStopUrl={body.get('oneStopUrl')}")
        one_stop_url = body.get("oneStopUrl")
        if one_stop_url:
            print(f"   ✅ 입장 가능 — oneStopUrl 발급됨: {one_stop_url}")
        return one_stop_url

    # 1단계: myRank 가 발급될 때까지 동시다발 요청
    while True:
        results = await asyncio.gather(*(_fetch_rank_once(req, waiting_id) for _ in range(RANK_BURST_SIZE)))
        bodies = [b for b in results if b]
        print("   [동시요청] totalRank=" + ", ".join(
            str(b.get("totalRank")) if b else "실패" for b in results))
        for body in bodies:
            if body.get("oneStopUrl"):
                return body, _report(body)
        ranked = next((b for b in bodies if b.get("myRank") is not None), None)
        if ranked:
            _report(ranked)
            break
        print(f"   myRank 미발급({len(bodies)}/{RANK_BURST_SIZE} 응답) — {RANK_BURST_GAP_S}초 후 재시도")
        await asyncio.sleep(RANK_BURST_GAP_S)

    # 2단계: myRank 발급 후에는 interval_s 간격 단건 폴링
    while True:
        await asyncio.sleep(interval_s)
        body = await _fetch_rank_once(req, waiting_id)
        if not body:
            continue
        one_stop_url = _report(body)
        if one_stop_url:
            return body, one_stop_url


async def click_product_for_auth_recovery(page, goods_code):
    """401 복구 때만 경기 목록의 예매하기를 한 번 클릭하고 대상 상품 화면을 기다린다."""
    if page.url.split("?", 1)[0].rstrip("/") != RECOVERY_LIST_URL:
        await page.goto(RECOVERY_LIST_URL, wait_until="domcontentloaded", timeout=30_000)

    month, day, hour, minute = RECOVERY_GAME_DATETIME
    date_pattern = re.compile(
        rf"^\s*{month}\s*월\s*{day}\s*일\s*"
        rf"(?:\(\s*[월화수목금토일]\s*\)\s*)?{hour:02d}\s*:\s*{minute:02d}\s*$"
    )
    date = page.get_by_text(date_pattern)
    expand = page.get_by_role("button", name=re.compile(r"^전체\s*보기$"))
    expanded = False
    deadline = time.monotonic() + 15
    while not await date.first.is_visible():
        if not expanded and await expand.count() == 1 and await expand.is_visible():
            await expand.click(timeout=10_000)
            expanded = True
        if time.monotonic() >= deadline:
            raise RuntimeError("인증 복구용 경기 날짜·시간을 목록에서 찾지 못했습니다.")
        await asyncio.sleep(0.2)
    if await date.count() != 1:
        raise RuntimeError("날짜·시간이 일치하는 경기가 여러 개여서 클릭을 중지합니다.")
    button = date.locator("xpath=ancestor::*[.//button][1]").get_by_role(
        "button", name="예매하기", exact=True
    )
    if await button.count() != 1:
        raise RuntimeError("대상 경기의 예매하기 버튼을 하나로 확정하지 못했습니다.")

    popups = []

    def on_popup(tab):
        popups.append(tab)

    page.on("popup", on_popup)
    try:
        await button.click(timeout=15_000)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            for tab in [page, *popups]:
                if tab.is_closed():
                    continue
                location = urlparse(tab.url)
                if location.hostname != "tickets.interpark.com":
                    continue
                path = location.path.rstrip("/")
                if not path.startswith("/gates/ticket/"):
                    continue
                if path != f"/gates/ticket/{goods_code}":
                    raise RuntimeError("클릭한 경기의 상품 코드가 설정과 다릅니다.")
                await tab.wait_for_load_state("load", timeout=15_000)
                await tab.get_by_role("button", name=re.compile(
                    r"예매하기|선예매|남은시간"
                )).first.wait_for(state="visible", timeout=15_000)
                return tab
            await asyncio.sleep(0.2)
        raise RuntimeError("상품 클릭 후 대상 상품 화면이 열리지 않았습니다.")
    finally:
        page.remove_listener("popup", on_popup)


async def run_line_up(page, goods_code=GOODS_CODE, biz_code=BIZ_CODE, place_code=PLACE_CODE,
                      channel_code=CHANNEL_CODE, lang=LANG, from_=FROM):
    """대기열 진입 체인을 순서대로 실행하고 결과 dict 를 반환한다.

    member-info → goods-info(오픈시각) → 오픈까지 대기 → secure-url → line-up
    → rank(oneStopUrl 폴링).
    각 단계는 개별 함수로 분리돼 있고, 실패 시 LineUpError 를 잡아 실패 dict 로 변환한다.
    """
    req = page.request  # 브라우저 컨텍스트의 쿠키/세션을 공유하는 API 클라이언트
    try:
        # 401이면 목록의 상품 버튼을 한 번 클릭해 인증을 복구하고, 실패하면 수동 입력을 기다린다.
        while True:
            try:
                member_info = await fetch_member_info(req, goods_code, channel_code)
                break
            except LineUpError as e:
                print(f"⚠️ member-info 실패(status={e.status} {e.body if e.body else ''})")
                if e.status == 401:
                    print("🔄 경기 목록의 예매하기 버튼을 한 번 클릭한 뒤 member-info 인증을 재확인합니다.")
                    try:
                        recovery_page = await click_product_for_auth_recovery(page, goods_code)
                        # 페이지 스크립트의 세션/쿠키 갱신 시간을 준 뒤 API 인증으로 복구를 판정한다.
                        await asyncio.sleep(2)
                        member_info = await fetch_member_info(req, goods_code, channel_code)
                    except LineUpError as retry_error:
                        print(f"⚠️ member-info 재확인 실패(status={retry_error.status} "
                              f"{retry_error.body if retry_error.body else ''}) — 자동 진행을 중지합니다.")
                    except Exception as recovery_error:
                        print(f"⚠️ 상품 버튼 클릭/인증 확인 실패({type(recovery_error).__name__}) "
                              "— 자동 진행을 중지합니다.")
                        if isinstance(recovery_error, RuntimeError):
                            print(f"   {recovery_error}")
                    else:
                        print("✅ member-info 인증 복구 확인 — Enter 입력 없이 자동 재개합니다.")
                        if recovery_page != page and not recovery_page.is_closed():
                            try:
                                await recovery_page.close()
                            except Exception:
                                print("⚠️ 인증 복구용 새 탭을 닫지 못했습니다.")
                        break
                    await send_telegram(
                        "⚠️ member-info 실패(status=401 Unauthorized)\n"
                        "상품 버튼 클릭을 통한 인증 복구에 실패하여 자동 진행을 중지했습니다.\n"
                        "로그인이 필요합니다. 브라우저에서 로그인 후 터미널에서 Enter를 눌러주세요."
                    )
                await wait_for_enter("🔐 로그인을 해주세요. 브라우저에서 로그인 후 Enter 를 누르세요... ")
        # 로그인 인증이 확인된 시점에 저장해 이후 중단돼도 다음 실행에서 재사용한다.
        try:
            await page.context.storage_state(path=SESSION_PATH)
            print(f"✅ 로그인 확인 후 세션 저장 완료: {SESSION_PATH}")
        except Exception as e:
            print(f"⚠️ 세션 저장 실패({type(e).__name__}) — 다음 실행에서 다시 로그인해야 할 수 있습니다.")
        goods_info = await fetch_goods_info(req, goods_code, biz_code, place_code, lang)
        skew_ms = await measure_clock_skew(req)   # 서버-로컬 시간차 측정
        ticket_open = goods_info.get("ticketOpenDate")
        warm_task = asyncio.create_task(_warm_before_open(req, ticket_open, skew_ms))
        # 일제발사 첫 발(-50ms)보다 살짝 먼저 대기가 끝나도록 lead_ms=100 으로 여유를 둔다.
        await wait_until_open(ticket_open, skew_ms=skew_ms, lead_ms=100)
        try:
            await asyncio.wait_for(asyncio.shield(warm_task), timeout=1.0)   # 예열이 끝났는지 확인(최대 1초)
        except Exception:
            pass
        redirect_url, key = await fetch_secure_url(
            req, member_info, biz_code, lang, from_,
            ticket_open_date=ticket_open, skew_ms=skew_ms)
        line_up = await fetch_line_up(req, key)
        rank, one_stop_url = await poll_rank(req, line_up["waitingId"])
    except LineUpError as e:
        print(f"❌ {e.step} 실패: status={e.status} {e.body if e.body else e.extra}")
        return {"ok": False, "step": e.step, "status": e.status, **e.extra}

    return {
        "ok": True,
        "memberInfo": member_info,
        "redirectUrl": redirect_url,
        "key": key,
        "lineUp": line_up,
        "waitingId": line_up["waitingId"],
        "rank": rank,
        "oneStopUrl": one_stop_url,
    }


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

        # 좌석 페이지 로딩 중 IssueCaptcha 요청이 바로 발생하므로 이동 전에 후킹한다.
        captcha_store = await attach_captcha_hook(page)

        # booking 의 [auto] 콘솔 로그를 터미널로 포워딩(좌석 자동선택 단계에서 사용).
        attach_console_forwarding(page)

        # page.request 가 올바른 Origin/쿠키 맥락을 갖도록 먼저 사이트에 접속해 둔다.
        await page.goto("https://nol.yanolja.com/ticket/genre/sports/postseason", wait_until="domcontentloaded", timeout=30_000)

        # 좌석 세션이 약 10분 뒤 끊기므로, 좌석을 못 잡은 채 시간이 다 되면 대기열부터 다시 진입한다.
        attempt = 0
        while True:
            attempt += 1
            print(f"\n🔁 진입 시도 #{attempt} | PC 시간: {datetime.now():%Y-%m-%d %H:%M:%S}")
            # page.request 가 올바른 Origin/쿠키 맥락을 갖도록 사이트에 접속해 둔다.
            await page.goto("https://nol.yanolja.com/ticket/genre/sports/postseason", wait_until="domcontentloaded", timeout=30_000)

            # ── 1단계: 대기열 진입 체인 (oneStopUrl 발급까지 폴링) ──
            result = await run_line_up(page)
            print("\n" + "=" * 60)
            one_stop_url = result.get("oneStopUrl") if result.get("ok") else None
            if not one_stop_url:
                print(f"❌ 대기열 진입 실패: step={result.get('step')} status={result.get('status')} — 5초 후 재시도")
                print("=" * 60)
                await asyncio.sleep(5)
                continue
            print("🎯 대기열 진입 체인 완료")
            print(f"   waitingId  : {result['waitingId']}")
            print(f"   oneStopUrl : {one_stop_url}")
            print("=" * 60)

            # ── 2단계: oneStopUrl 로 이동 → 좌석 자동선택(booking) ──
            print(f"\n🌐 좌석 페이지로 이동: {one_stop_url}")
            # 이전 진입의 토큰을 재사용하지 않고, 이번 페이지 로딩 응답을 받는다.
            captcha_store["encRnd"] = None
            captcha_store["captchaId"] = None
            captcha_store["audio"] = None
            captcha_store["audioMimeType"] = None
            await page.goto(one_stop_url, wait_until="domcontentloaded", timeout=30_000)

            booking = await run_booking(page, enc_store=captcha_store)
            if booking.get("ok"):
                break   # 서버에서 좌석 확보가 확인된 경우
            if booking.get("requires_review"):
                print("⚠️ 좌석 확보 여부 불명 — 중복 예약 시도를 멈춥니다. 브라우저에서 직접 확인하세요.")
                break
            print(f"⏱ 좌석 미확보({booking.get('reason')}) — 대기열부터 다시 진입합니다.")
            await asyncio.sleep(1)

        # 결제를 사람이 마치도록 대기. 엔터를 누르면 세션 저장 후 종료한다.
        await wait_for_enter("\n⏳ 브라우저 상태 확인 및 필요한 결제를 마친 후 Enter 를 누르면 종료합니다... ")

        await context.storage_state(path=SESSION_PATH)
        print(f"✅ 세션 저장 완료: {SESSION_PATH}")
        await context.close()
        await browser.close()
        print("🔚 브라우저 종료")


if __name__ == "__main__":
    with terminal_log("line_up"):
        asyncio.run(main())
