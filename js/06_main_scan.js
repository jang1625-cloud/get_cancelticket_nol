// ── 메인 실행 본문(scan): 블록 우선순위대로 좌석 상태를 조회 → 통로쪽 연석 즉시 선점 ──
// ctx: preferBlocks(예 ["110","330","328"] 또는 "001:110"), ticketCount, minTicketCount, pollIntervalMs, maxPollRounds
//   blockKey = `${playSeq}:${블록코드}` (예 "001:110")
// 반환: { ok:true, blockKey, seats:[...] } | { ok:false, reason:"timeout" | "no-prefer-blocks" }
const blockKeys = (preferBlocks || []).map(String).map((b) => (b.includes(":") ? b : `${playSeq}:${b}`));
if (!blockKeys.length) return { ok: false, reason: "no-prefer-blocks" };

// 등급별 인원 고정(allocatedSeatCount)·등급명(휠체어 판별)·블록별 등급 매핑은 한 번만 조회.
await loadGradeMaps();
const gradeInfo = gradeMaps.info;
// 휠체어/장애인석 판별: 좌석 등급명·remain 등급명에 "휠체어"/"장애" 포함, 또는 좌석번호가 w+숫자(예 "w10").
const WHEELCHAIR_RE = /휠체어|장애/;
const isWheelchair = (s) =>
  WHEELCHAIR_RE.test(String(s.seatGradeName || "")) ||
  WHEELCHAIR_RE.test(String((gradeInfo[String(s.seatGrade)] || {}).name || "")) ||
  /^w\d+$/i.test(String(s.seatNo || ""));

const gradeFails = {};  // 등급별 preselect 거절 횟수. 2회 이상이면 그 등급은 건너뜀(예약 불가 상품일 가능성)
const bad = new Set();   // preselect 가 거절한 좌석: 다시 시도하지 않는다(진단 로그는 1회만)
// 매수 우선순위: 블록별로 ticketCount(최대) → … → minTicketCount(최소) 순으로 시도하고 다음 블록으로 넘어간다. (인원 고정 상품은 allocatedSeatCount 가 우선이라 영향 없음)
const minCount = Math.max(1, Math.min(minTicketCount || ticketCount, ticketCount));
const counts = [];
for (let c = ticketCount; c >= minCount; c--) counts.push(c);
log("scan 시작 blocks=" + JSON.stringify(blockKeys) + " count=" + JSON.stringify(counts));

// 열 우선순위: place 26001228 의 1xx 블록은 13열 이상을 먼저, 없으면 그 앞 열(1~12열)을 시도한다.
//   열 번호는 rowNo 의 "N열" 에서 읽는다(읽을 수 없으면 선호 열로 취급).
const rowNum = (rowNo) => { const m = /(\d+)\s*열/.exec(String(rowNo)); return m ? parseInt(m[1], 10) : null; };
const isRowSensitive = (bk) => String(placeCode) === "26001228" && /:1\d\d$/.test(bk);
const backRows = (rowNo) => { const n = rowNum(rowNo); return n === null || n >= 13; };
const frontRows = (rowNo) => { const n = rowNum(rowNo); return n !== null && n < 13; };

// tryBlock(blk, rowFilter, count) : 블록 안 등급별 통로쪽 연석 후보를 구해 가까운 순으로 선점 시도. 성공 시 결과 객체, 아니면 null.
const tryBlock = async (blk, rowFilter, count) => {
  const bk = blk.blockKey;
  // 블록 안의 등급별로 통로쪽 연석 후보를 구해 가장 통로에 가까운 순으로 시도(휠체어석 제외).
  const grades = [...new Set(blk.seats.filter((s) => !isWheelchair(s)).map((s) => String(s.seatGrade)))]
    .filter((g) => (gradeFails[g] || 0) < 2);
  const cands = grades
    .map((g) => pickByAisle(blk, g, count, (gradeInfo[g] || {}).allocated || 0, rowFilter))
    .filter(Boolean)
    .sort((a, b) => a.score - b.score || a.tie - b.tie);
  for (const c of cands) {
    log(`시도: block=${bk} 등급=${c.seats[0].seatGrade} 통로거리=${c.score} 좌석=` + c.seats.map((s) => s.rowNo + " " + s.seatNo).join(", "));
    const res = await tryReserve(blk, c.seats);
    if (!res.ok && res.reason === "preselect") {
      for (const s of c.seats) bad.add(s.seatInfoId);
      const gg = String(c.seats[0].seatGrade);
      gradeFails[gg] = (gradeFails[gg] || 0) + 1;
      if (gradeFails[gg] === 2) log("등급 " + gg + " 는 preselect 가 반복 거절돼 제외합니다");
      log("진단 seat=" + JSON.stringify(c.seats[0]) + " gradeInfo=" + JSON.stringify(gradeMaps.info[String(c.seats[0].seatGrade)]) +
          " blockGrades=" + JSON.stringify([...(gradeMaps.byBlock[bk] || [])]));
    }
    if (res.ok) {
      log("✅ 좌석 확보 성공!", res.seats.map((s) => s.seatInfoId));
      syncSelectionToUI(bk, res.seats);
      return {
        ok: true, blockKey: bk,
        seats: res.seats.map((s) => ({
          seatInfoId: s.seatInfoId, seatGrade: s.seatGrade,
          rowNo: s.rowNo, seatNo: s.seatNo, price: s.salesPrice,
        })),
      };
    }
    log("실패, 다음 후보로", res.reason);
  }
  return null;
};

for (let round = 0; round < maxPollRounds && !(deadlineMs && Date.now() >= deadlineMs); round++) {
  // 우선 블록 전체를 seatMeta/seatStatus 각 1회로 한꺼번에 조회
  const blks = await fetchBlocks(blockKeys);
  const usable = [];
  for (const blk of blks) {
    const bk = blk.blockKey;
    if (!blk.seats.length || !blk.bits) {
      if (round === 0) log("block=" + bk + " 좌석정보 없음(seats=" + blk.seats.length + ")");
      continue;
    }
    if (round === 0) {   // 진단: 비트맵 정렬 확인 (seats.length 와 bitsLen 이 다르거나 등급별 '1' 개수가 잔여수와 크게 다르면 어긋난 것)
      const cnt = {};
      blk.seats.forEach((s, i) => { if (blk.bits[i] === "1") cnt[s.seatGrade] = (cnt[s.seatGrade] || 0) + 1; });
      log(`진단 block=${bk} seats=${blk.seats.length} bitsLen=${blk.bits.length} bit1=${(blk.bits.match(/1/g) || []).length} ` +
          `등급별bit1=${JSON.stringify(cnt)} 등급별잔여=${JSON.stringify(Object.fromEntries([...(gradeMaps.byBlock[bk] || [])].map((g) => [g, (gradeMaps.info[g] || {}).remain])))}`);
    }
    for (let i = 0; i < blk.seats.length; i++) if (bad.has(blk.seats[i].seatInfoId)) blk.bits = blk.bits.slice(0, i) + "0" + blk.bits.slice(i + 1);
    usable.push(blk);
  }
  // 1차: 일반 블록은 전체, 1xx 블록(26001228)은 13열 이상만.  2차: 1xx 블록의 앞 열(1~12열).
  for (const pass of [0, 1]) {
    for (const blk of usable) {
      const sens = isRowSensitive(blk.blockKey);
      if (pass === 1 && !sens) continue;
      // 블록마다 최대 매수 → 최소 매수 순으로 시도한 뒤 다음 블록으로 넘어간다.
      for (const count of counts) {
        const found = await tryBlock(blk, sens ? (pass === 0 ? backRows : frontRows) : null, count);
        if (found) return found;
      }
    }
  }
  if (round % 10 === 0) log(`round ${round}: 우선 블록에 연석 없음, 재조회…`);
  await sleep(pollIntervalMs);
}
return { ok: false, reason: "timeout" };   // 시간 초과 → 호출자가 대기열부터 재진입
