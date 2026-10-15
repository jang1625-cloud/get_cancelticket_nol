"""블록 좌석 배치를 OX 로 그려 보여주는 도구 (Playwright page.request 로 호출).

사용:
  - 실행하면 브라우저를 띄우고(세션 로드) tickets.interpark.com 에 접속한 뒤,
    사용자로부터 블록(예: 101 또는 001:101)을 입력받아
    해당 블록의 좌석 예매 여부를 O(예매가능)/X(점유) 격자로 출력한다.
  - 좌석 조회는 seatMeta(좌석목록: rowIdx/colIdx/좌석정보) + seatStatus(hex 비트맵)를
    page.request 로 직접 호출한다. 비트 '1'=예매가능, '0'=점유.
  - seatMeta 의 seats 배열 순서와 seatStatus 비트열은 1:1 대응하며,
    각 좌석의 rowIdx/colIdx 로 격자 좌표에 배치해 그린다.

참고:
  - 좌석 API 는 좌석 페이지(/onestop/seat) 세션이 있어야 정상 응답할 수 있다.
    단독 실행 시엔 아래 상수(GOODS_CODE 등)를 실제 값으로 맞춰야 하며,
    권한/세션이 없으면 빈 결과가 나올 수 있다.
"""

import asyncio
import os

from patchright.async_api import async_playwright
from browser_config import SESSION_PATH, get_launch_options

# 대상 정보 (실제 값으로 맞추기). 좌석 페이지 URL 쿼리/컨텍스트에서 확인 가능.
GOODS_CODE = "26014409"
PLACE_CODE = "26001228"
PLAY_SEQ = "001"
BIZ_CODE = "61776"

TICKETS_ORIGIN = "https://tickets.interpark.com"

CONTEXT_OPTIONS = {
    "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "viewport": {"width": 1280, "height": 900},
    "locale": "ko-KR",
    "timezone_id": "Asia/Seoul",
}


def _normalize_block_key(raw: str) -> str:
    """사용자 입력 블록을 blockKey 형식(playSeq:block)으로 정규화.

    입력 예) "101", "001:101", " 101 " → "001:101"
    """
    s = raw.strip()
    if ":" in s:
        return s
    return f"{PLAY_SEQ}:{s}"


def _hex_to_bits(hex_str: str) -> str:
    """seatStatus hex → '1'/'0' 비트열 (seatMeta seats 순서와 1:1 대응)."""
    return "".join(bin(int(ch, 16))[2:].zfill(4) for ch in hex_str)


async def fetch_remain(req, goods_code=GOODS_CODE, play_seq=PLAY_SEQ):
    """등급별 잔여석 조회(gql GetRemainSeats). blockKey 목록 확인용.
    반환: getRemainSeats 리스트 (실패 시 빈 리스트)."""
    query = ("query GetRemainSeats($input: GetRemainSeatSortInput) { "
             "getRemainSeats(input: $input) { remainSeatCount seatGrade seatGradeName blockKey } }")
    res = await req.post(
        f"{TICKETS_ORIGIN}/onestop/gql",
        headers={"content-type": "application/json", "x-requested-with": "XMLHttpRequest"},
        data={"query": query,
              "variables": {"input": {"sort": "SEAT_GRADE_ASC", "playSeq": play_seq}},
              "operationName": "GetRemainSeats"},
    )
    if not res.ok:
        print(f"⚠️ getRemainSeats 실패: {res.status}")
        return []
    body = await res.json()
    return (body.get("data") or {}).get("getRemainSeats") or []


async def fetch_block(req, block_key, goods_code=GOODS_CODE, place_code=PLACE_CODE,
                      play_seq=PLAY_SEQ, biz_code=BIZ_CODE):
    """블록의 seatMeta(좌석목록) + seatStatus(비트맵)를 함께 조회.
    반환: {"blockKey", "seats":[...], "bits":"1010.."} (seats 순서 == bits 순서)."""
    params = {
        "goodsCode": goods_code,
        "placeCode": place_code,
        "playSeq": play_seq,
        "blockKeys": block_key,
        "bizCode": biz_code,
    }
    headers = {"x-requested-with": "XMLHttpRequest", "referer": f"{TICKETS_ORIGIN}/onestop/seat"}

    meta_res, status_res = await asyncio.gather(
        req.get(f"{TICKETS_ORIGIN}/onestop/api/seatMeta", params=params, headers=headers),
        req.get(f"{TICKETS_ORIGIN}/onestop/api/seatStatus", params=params, headers=headers),
    )

    seats = []
    if meta_res.ok:
        meta_body = await meta_res.json()
        if isinstance(meta_body, list):
            blk = next((b for b in meta_body if b.get("blockKey") == block_key), None)
            seats = blk.get("seats", []) if blk else []

    bits = ""
    if status_res.ok:
        status_body = await status_res.json()
        data = status_body.get("data") if isinstance(status_body, dict) else None
        if data:
            bits = _hex_to_bits(data[0] or "")

    return {"blockKey": block_key, "seats": seats, "bits": bits}


def _grade_label(code, name):
    """등급 코드/이름을 'code(name)' 형태의 라벨로. 한쪽만 있으면 그것만."""
    code = str(code).strip() if code is not None else ""
    name = str(name).strip() if name else ""
    if code and name:
        return f"{code}({name})"
    return code or name


def _summarize_grades(seats):
    """seatMeta seats 에 등장하는 등급 코드(seatGrade)+이름을 중복 없이 코드순 정렬해 반환."""
    found = {}
    for s in seats:
        code = s.get("seatGrade")
        name = s.get("seatGradeName")
        if code is None and not name:
            continue
        found.setdefault(str(code) if code is not None else name, name)
    return [_grade_label(c if c != n else None, n) for c, n in sorted(found.items())]


def summarize_block_grades(remain):
    """getRemainSeats 결과(등급→블록목록)를 블록→등급코드 목록으로 뒤집어 요약.
    반환: {블록번호: [(seatGrade, seatGradeName, remainSeatCount), ...]} (블록번호 정렬)."""
    by_block = {}
    for row in remain:
        code = row.get("seatGrade")
        name = row.get("seatGradeName")
        cnt = row.get("remainSeatCount") or 0
        for bk in row.get("blockKey") or []:
            blk = bk.split(":")[-1]
            by_block.setdefault(blk, []).append((code, name, cnt))
    return dict(sorted(by_block.items()))


def render_block(block):
    """블록의 좌석을 rowIdx/colIdx 격자에 O/X 로 그려 문자열로 반환.

      O = 예매가능(비트 '1'), X = 점유/불가(비트 '0'), 공백 = 좌석 없음.
    """
    seats = block["seats"]
    bits = block["bits"]
    if not seats:
        return f"[{block['blockKey']}] 좌석 정보 없음 (세션/권한/블록번호 확인)"
    if not bits:
        return f"[{block['blockKey']}] 상태(seatStatus) 정보 없음"

    # 좌석마다 (rowIdx, colIdx) 와 상태(bit) 매핑
    max_row = max((s.get("rowIdx") or 0) for s in seats)
    max_col = max((s.get("colIdx") or 0) for s in seats)

    # 격자 초기화 (공백)
    grid = [[" " for _ in range(max_col + 1)] for _ in range(max_row + 1)]
    avail = 0
    total = 0
    for i, s in enumerate(seats):
        r = s.get("rowIdx")
        c = s.get("colIdx")
        if r is None or c is None:
            continue
        # 비트 인덱스가 범위를 벗어나면 점유로 간주
        is_avail = i < len(bits) and bits[i] == "1"
        grid[r][c] = "O" if is_avail else "X"
        total += 1
        if is_avail:
            avail += 1

    # 열 머리글(colIdx) — 10 단위만 표시
    lines = []
    grades = _summarize_grades(seats)
    lines.append(f"블록 {block['blockKey']}  |  좌석 {total}석  |  예매가능(O) {avail} / 점유(X) {total - avail}")
    if grades:
        lines.append("등급(코드): " + ", ".join(grades))
    lines.append("(O=예매가능, X=점유/불가, 공백=좌석없음)  ※ 화면 방향과 다를 수 있음")
    lines.append("")

    # 행 출력 (rowIdx 0 이 위). 각 행 앞에 rowIdx 표기.
    for r, row in enumerate(grid):
        lines.append(f"{r:>3} | " + "".join(row))
    return "\n".join(lines)


async def prompt_and_render(page):
    """사용자로부터 블록을 반복 입력받아 OX 격자를 출력한다. 빈 입력이면 종료."""
    req = page.request
    loop = asyncio.get_event_loop()

    # 참고용으로 잔여>0 블록 목록을 먼저 보여준다.
    remain = await fetch_remain(req)
    if remain:
        print("\n잔여>0 블록(참고):")
        for row in remain:
            if (row.get("remainSeatCount") or 0) > 0:
                blks = ", ".join(bk.split(":")[-1] for bk in (row.get("blockKey") or []))
                print(f"  등급 {row.get('seatGrade')} ({row.get('seatGradeName')}) "
                      f"잔여 {row.get('remainSeatCount')} → 블록 {blks}")

        # 블록 기준으로 뒤집어, 각 블록에서 가능한 등급 코드를 요약한다.
        by_block = summarize_block_grades(remain)
        if by_block:
            print("\n블록별 가능 등급 코드(참고):")
            for blk, grades in by_block.items():
                labels = ", ".join(
                    _grade_label(c, n) + (f" 잔여{cnt}" if cnt else "")
                    for c, n, cnt in sorted(grades, key=lambda g: str(g[0])))
                print(f"  블록 {blk} → {labels}")

    while True:
        raw = await loop.run_in_executor(
            None, input, "\n조회할 블록 번호 입력 (예: 101, 빈 입력이면 종료): ")
        if not raw.strip():
            print("종료합니다.")
            return
        block_key = _normalize_block_key(raw)
        print(f"→ {block_key} 조회 중…")
        block = await fetch_block(req, block_key)
        print()
        print(render_block(block))


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

        # page.request 가 올바른 Origin/쿠키 맥락을 갖도록 먼저 사이트에 접속해 둔다.
        await page.goto(f"{TICKETS_ORIGIN}/", wait_until="domcontentloaded", timeout=30_000)

        await prompt_and_render(page)

        await context.close()
        await browser.close()
        print("🔚 브라우저 종료")


if __name__ == "__main__":
    asyncio.run(main())
