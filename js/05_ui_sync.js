// syncSelectionToUI(blockKey, seats) : API 로 확보한 좌석을 화면(React)에 반영하고
//   가격 단계로 이동한다. context.seats 를 프론트 좌석 객체 형태로 채우고,
//   선점 만료 타이머를 세팅한 뒤 "/onestop/seat?step=price" 로 라우팅한다.
//   파라미터:
//     blockKey = "001:101"
//     seats    = tryReserve().seats (좌석 객체 배열; seatInfoId/seatGrade/rowNo/seatNo/salesPrice 등)
//   반환: undefined (side effect: sessionStorage 갱신 + 300ms 뒤 페이지 이동)
const syncSelectionToUI = (blockKey, seats) => {
  try {
    const c = readCtx();
    // 좌석 객체 형태(프론트 iJ 클래스 필드)에 맞춰 채운다.
    c.seats = seats.map((s) => ({
      blockKey,
      seatInfoId: s.seatInfoId,
      seatGrade: String(s.seatGrade),
      seatGradeName: s.seatGradeName || "",
      salesPrice: s.salesPrice,
      floor: s.floor || "",
      rowNo: s.rowNo,
      seatNo: s.seatNo,
      posLeft: s.posLeft,
      posTop: s.posTop,
      isSelectable: false,
      seatGroupId: s.seatGroupId || null,
    }));
    sessionStorage.setItem(CTX_KEY, JSON.stringify(c));
    log("context.seats 주입 완료 (" + c.seats.length + "석)");

    // 선점 만료 타이머 키(프론트 참조). 없으면 넉넉히 7분으로 세팅.
    try {
      const EXP_KEY = "interpark/reserveExpireTime";
      if (!sessionStorage.getItem(EXP_KEY))
        sessionStorage.setItem(EXP_KEY, String(Date.now() + 7 * 60 * 1000));
    } catch {}

    // evaluate 가 결과를 정상 반환한 뒤 이동하도록 살짝 지연(즉시 navigate 시 예외).
    log("가격 단계로 이동 예약: /onestop/seat?step=price");
    setTimeout(() => { location.assign("/onestop/seat?step=price"); }, 300);
  } catch (e) {
    log("syncSelectionToUI 예외:", String(e));
  }
};
