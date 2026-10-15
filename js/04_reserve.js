const preselectMut = `mutation PreselectSeat($command: PreselectSeatCommand!) { preselectSeat(command: $command) }`;
const deselectMut  = `mutation BulkDeselectSeats($command: BulkDeselectSeatsCommand!) { bulkDeselectSeats(command: $command) }`;

// deselect(ids) : 선점한 좌석들을 해제(실패 후 다음 시도 방해 방지). 반환값 없음.
//   파라미터 예) ["26004559:26000084:001:522", "26004559:26000084:001:523"]
//   반환: Promise<undefined>  (빈 배열이면 아무것도 안 함)
const deselect = async (ids) => {
  if (!ids.length) return;
  try { await gql(deselectMut, { command: { seatInfoIds: ids } }, "BulkDeselectSeats"); } catch {}
};

// tryReserve(blk, chosen) : 좌석들을 하나씩 preselect → seats/select 로 확정.
//   중간 실패 시 이미 선점한 좌석은 deselect 로 되돌린다.
//   파라미터:
//     blk    = fetchBlock() 결과 (blk.blockKey 사용)
//     chosen = pickSeats() 결과 (좌석 객체 배열)
//     alreadyPreselected = (선택) 이미 선점된 seatInfoId. preselect 를 건너뛴다.
//   반환:
//     성공 예) { ok: true, seats: [ {seatInfoId,...}, {seatInfoId,...} ] }
//     실패 예) { ok: false, reason: "preselect" }
//          또는 { ok: false, reason: "select", status: 400 }
const tryReserve = async (blk, chosen, alreadyPreselected) => {
  const done = [];
  for (const s of chosen) {
    if (alreadyPreselected && s.seatInfoId === alreadyPreselected) { continue; }   // 사용자가 클릭해 이미 선점됨
    const r = await gql(preselectMut, {
      command: { playSeq, blockKey: blk.blockKey, seatGrade: String(s.seatGrade), seatInfoId: s.seatInfoId },
    }, "PreselectSeat");
    if (!(r.body && r.body.data && r.body.data.preselectSeat === true)) {
      log("preselect 실패", s.seatInfoId, "status=" + r.status,
          "err=" + JSON.stringify(r.body && r.body.errors ? r.body.errors : r.body));
      await deselect(done.map((x) => x.seatInfoId));
      return { ok: false, reason: "preselect" };
    }
    done.push(s);
  }
  const selBody = {
    goodsCode, placeCode, playSeq, seatType: "SPORTS",
    seats: chosen.map((s) => ({ seatGrade: String(s.seatGrade), seatInfoId: s.seatInfoId })),
    sessionId, autoAssign: false,
  };
  const sel = await j("/onestop/api/seats/select", { method: "POST", body: JSON.stringify(selBody) });
  const bad = sel.body && sel.body.unselectableSeatInfoIds;
  if (sel.status >= 200 && sel.status < 300 && Array.isArray(bad) && bad.length === 0) {
    return { ok: true, seats: chosen };
  }
  log("select 실패", "status=" + sel.status, "body=" + JSON.stringify(sel.body));
  await deselect(done.map((x) => x.seatInfoId));
  return { ok: false, reason: "select", status: sel.status };
};
