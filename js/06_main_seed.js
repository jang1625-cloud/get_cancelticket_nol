// ── 메인 실행 본문: 사용자가 첫 좌석을 클릭 → 그 좌석 기준 좌우 연석 자동 확장 ──
// ctx 추가값: seedSeatInfoId(사용자가 클릭한 좌석), seedBlockKey, seedGrade
// 반환: { ok:true, blockKey, seats:[{seatInfoId,seatGrade,rowNo,seatNo,price}] }
//       { ok:false, reason: "no-adjacent"(전부 해제됨) | "timeout" | "wheelchair"(해제됨) | "no-seed" }
{
  const c = readCtx();
  log("JS 시작(연석 자동확장). sessionId=" + (c.sessionId || sessionId || "?") +
      " seed=" + seedSeatInfoId + " block=" + seedBlockKey + " count=" + ticketCount);
}
if (!seedSeatInfoId || !seedBlockKey) return { ok: false, reason: "no-seed" };
await loadGradeMaps();

// 연석이 failAfterRounds 번 연속 안 되면 선점 좌석(첫 클릭 좌석 포함)을 모두 풀고 실패 반환.
let fails = 0;
const releaseAll = async (extraIds) => {
  await deselect([seedSeatInfoId, ...(extraIds || [])]);
  log("선점 좌석 모두 해제");
};
for (let round = 0; round < maxPollRounds; round++) {
  const blk = await fetchBlock(seedBlockKey);
  if (round === 0 || round % 10 === 0) {
    const si = blk.seats.findIndex((s) => s.seatInfoId === seedSeatInfoId);
    const sd = si >= 0 ? blk.seats[si] : null;
    log(`round ${round}: seats=${blk.seats.length} bitsLen=${blk.bits.length} seedIdx=${si}` +
        (sd ? ` seed=${sd.rowNo} ${sd.seatNo}번 grade=${sd.seatGrade} bit=${blk.bits[si]}` : ""));
    if (sd) {
      const near = blk.seats.map((s, i) => ({ s, i })).filter((x) => x.s.rowNo === sd.rowNo)
        .sort((a, b) => parseInt(a.s.seatNo, 10) - parseInt(b.s.seatNo, 10))
        .filter((x) => Math.abs(parseInt(x.s.seatNo, 10) - parseInt(sd.seatNo, 10)) <= 3)
        .map((x) => `${x.s.seatNo}:bit${blk.bits[x.i]}:g${x.s.seatGrade}:exp${x.s.isExposable}`);
      log("seed 주변: " + near.join(" | "));
    }
  }
  if (!blk.seats.length || !blk.bits) { await sleep(pollIntervalMs); continue; }
  const seedSeat = blk.seats.find((s) => s.seatInfoId === seedSeatInfoId);
  if (seedSeat && /휠체어|장애/.test(String(seedSeat.seatGradeName || ""))) {
    log("휠체어석은 제외 대상: " + seedSeat.seatGradeName);
    await releaseAll();
    return { ok: false, reason: "wheelchair" };
  }
  // ticketCount → minTicketCount 순으로 줄여가며 좌우 연석 확장
  let picked = null;
  const lowest = Math.max(1, Math.min(minTicketCount || ticketCount, ticketCount));
  for (let cnt = ticketCount; cnt >= lowest && !picked; cnt--) {
    picked = cnt > 1 ? pickAdjacent(blk, seedSeatInfoId, cnt)
                     : blk.seats.filter((s) => s.seatInfoId === seedSeatInfoId);
  }
  if (!picked || picked.length < 1) {
    if (round % 10 === 0) log(`round ${round}: 좌우 연석 ${ticketCount}석 확보 불가, 재조회…`);
    if (++fails >= failAfterRounds) { await releaseAll(); return { ok: false, reason: "no-adjacent" }; }
    await sleep(pollIntervalMs);
    continue;
  }
  log("시도: " + picked.map((s) => s.rowNo + " " + s.seatNo).join(", "));
  const res = await tryReserve(blk, picked, seedSeatInfoId);
  if (res.ok) {
    log("✅ 연석 확보 성공!", res.seats.map((s) => s.seatInfoId));
    syncSelectionToUI(seedBlockKey, res.seats);
    return {
      ok: true, blockKey: seedBlockKey,
      seats: res.seats.map((s) => ({
        seatInfoId: s.seatInfoId, seatGrade: s.seatGrade,
        rowNo: s.rowNo, seatNo: s.seatNo, price: s.salesPrice,
      })),
    };
  }
  log("실패, 재시도", res.reason);
  if (++fails >= failAfterRounds) { await releaseAll(); return { ok: false, reason: "no-adjacent" }; }
  await sleep(pollIntervalMs);
}
return { ok: false, reason: "timeout" };
