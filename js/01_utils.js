// log(...args) : 콘솔에 "[auto]" 접두사로 출력. 객체 인자는 JSON 문자열로 변환.
//   예) log("시도", { a: 1 })   → 콘솔: [auto] 시도 {"a":1}   / 반환: undefined
const log = (...a) => console.log("[auto]", ...a.map((x) =>
  typeof x === "object" ? JSON.stringify(x) : x));

// sleep(ms) : ms 밀리초 대기하는 Promise.
//   예) await sleep(300)   → 반환: Promise<undefined> (0.3초 뒤 resolve)
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const CTX_KEY = "interpark/context";
const LANG_MAP = { ko: "KO", en: "EN", ja: "JA", "zh-cn": "ZH_CN", zh: "ZH_CN" };

// readCtx() : sessionStorage["interpark/context"] 를 파싱해 객체로 반환. 없으면 {}.
//   파라미터: 없음
//   반환 예) {
//     channelType: "NOL",
//     sessionId: "26004559_M0000000581471786254843",
//     entMemberCode: "izdiRmUezkXv72KprY9yuw==",
//     bizCode: "61776",
//     lang: "ko",
//     playSeq: "001",
//     goods: { preOpt: "dkSex1Ps...cks3", isInterlocking: false, ... },
//     seats: []            // 선택 좌석(우리가 syncSelectionToUI 에서 채움)
//   }
const readCtx = () => {
  try { return JSON.parse(sessionStorage.getItem(CTX_KEY) || "{}"); }
  catch { return {}; }
};

// randTrace(n) : 영숫자 n자 랜덤 문자열. X-OneStop-Trace-ID 헤더용(매 요청 새로).
//   파라미터: n = 16 (자릿수)
//   반환 예) "7qPzu8cpuB5ENQdi"   (길이 n 의 문자열)
const randTrace = (n) => {
  const cs = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  let s = "";
  for (let i = 0; i < n; i++) s += cs[Math.floor(Math.random() * cs.length)];
  return s;
};

// hexToBits(hex) : seatStatus 의 hex 문자열을 '1'/'0' 비트열로 펼친다.
//   seatMeta 의 seats 배열과 '같은 순서로' 1:1 대응한다(비트 '1' = 예매가능).
//   파라미터 예) "FFFFFF8003..."   (hex 문자열)
//   반환   예) "111111111111111110000000000000110..."  (길이 = hex길이 × 4)
const hexToBits = (hex) =>
  Array.from(hex).map((ch) => parseInt(ch, 16).toString(2).padStart(4, "0")).join("");
