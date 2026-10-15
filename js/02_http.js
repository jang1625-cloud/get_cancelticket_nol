// buildHeaders(extra?) : 인터파크 axios 인터셉터가 심는 요청 헤더를 재현해 반환.
//   readCtx() 값 + 매 요청 랜덤 Trace-ID 로 구성. extra 로 개별 헤더 덮어쓰기 가능.
//   (이 헤더, 특히 X-OneStop-Trace-ID 가 빠지면 seats/select 가 400)
//   파라미터 예) { "Content-Type": "text/plain" }  (선택, 병합/덮어쓰기용)
//   반환 예) {
//     "Content-Type": "application/json",
//     "X-Requested-With": "XMLHttpRequest",
//     "X-OneStop-Trace-ID": "7qPzu8cpuB5ENQdi",
//     "X-Onestop-Channel": "NOL",
//     "X-Onestop-Session": "26004559_M0000000581471786254843",
//     "X-Ticket-BFF-Language": "KO",
//     "Accept": "application/json, text/plain, */*",
//     "Authorization": "Bearer dkSex1Ps...cks3"     // preOpt/token 있을 때만
//   }
const buildHeaders = (extra) => {
  const c = readCtx();
  const h = {
    "Content-Type": "application/json",
    "X-Requested-With": "XMLHttpRequest",
    "X-OneStop-Trace-ID": randTrace(16),
    "X-Onestop-Channel": c.channelType || "ONESTOP",
    "X-Onestop-Session": c.sessionId || sessionId || "session_undefined",
    "X-Ticket-BFF-Language": LANG_MAP[(c.lang || "ko").toLowerCase()] || "KO",
    "Accept": "application/json, text/plain, */*",
  };
  const auth = (c.goods && c.goods.preOpt) || token;
  if (auth) h["Authorization"] = "Bearer " + auth;
  return { ...h, ...(extra || {}) };
};

// j(url, opts?) : 인터셉터 헤더를 실어 fetch 하고 {status, body} 로 반환.
//   body 는 JSON 이면 파싱된 객체, 아니면 원문 문자열.
//   파라미터 예) j("/onestop/api/seatMeta?...", { method: "GET" })
//   반환 예) { status: 200, body: [ { blockKey: "001:101", seats: [...] } ] }
//        또는 { status: 400, body: { statusCode: 400 } }
const j = async (url, opts = {}) => {
  const res = await fetch(url, {
    credentials: "include",
    headers: buildHeaders(opts.headers),
    ...opts,
  });
  const text = await res.text();
  try { return { status: res.status, body: JSON.parse(text) }; }
  catch { return { status: res.status, body: text }; }
};

// gql(query, variables, operationName) : /onestop/gql 로 GraphQL POST. {status, body} 반환.
//   파라미터 예) gql(remainQuery, { input: { sort: "SEAT_GRADE_ASC", playSeq: "001" } }, "GetRemainSeats")
//   반환 예) { status: 200, body: { data: { getRemainSeats: [...] } } }
//        또는 { status: 200, body: { errors: [ { message: "..." } ], data: null } }
const gql = (query, variables, operationName) =>
  j("/onestop/gql", {
    method: "POST",
    body: JSON.stringify({ query, variables, operationName }),
  });
