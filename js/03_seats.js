const remainQuery = `query GetRemainSeats($input: GetRemainSeatSortInput) {
  getRemainSeats(input: $input) { remainSeatCount seatGrade seatGradeName blockKey }
}`;

// fetchRemain() : 등급별 잔여석을 조회한다.
//   파라미터: 없음 (상위 스코프의 playSeq 사용)
//   반환 예) {
//     status: 200,
//     list: [
//       { seatGrade: "12", seatGradeName: "1루 레드석", remainSeatCount: 213, blockKey: ["001:101","001:102"] },
//       { seatGrade: "14", seatGradeName: "1루 네이비석", remainSeatCount: 23, blockKey: ["001:302"] },
//       ...
//     ],
//     errors: undefined            // gql 에러 시 [{ message: "..." }]
//   }
const fetchRemain = async () => {
  const r = await gql(remainQuery, { input: { sort: "SEAT_GRADE_ASC", playSeq } }, "GetRemainSeats");
  const list = (r.body && r.body.data && r.body.data.getRemainSeats) || [];
  return { status: r.status, list, errors: r.body && r.body.errors };
};

// collectCandidateBlocks(list, grades) : 잔여>0 블록을 선호등급 우선순위로 수집(중복 제거).
//   파라미터:
//     list   = fetchRemain().list
//     grades = ["12","14"]   (빈 배열이면 전 등급, 잔여>0 순서대로)
//   반환 예) ["001:101", "001:102", "001:302"]   (blockKey 배열, grades 순 → 잔여순)
const collectCandidateBlocks = (list, grades) => {
  const out = [];
  const order = grades.length ? grades : [...new Set(list.map((x) => String(x.seatGrade)))];
  for (const g of order)
    for (const row of list) {
      if (String(row.seatGrade) !== g) continue;
      if ((row.remainSeatCount || 0) <= 0) continue;
      for (const bk of (row.blockKey || [])) out.push(bk);
    }
  return [...new Set(out)];
};

// fetchBlock(blockKey) : 한 블록의 좌석목록(seatMeta) + 점유 비트맵(seatStatus)을 함께 조회.
//   seats 배열 순서와 bits 문자열이 1:1 대응한다(bits[i] === seats[i] 상태).
//   파라미터 예) "001:101"
//   반환 예) {
//     blockKey: "001:101",
//     seats: [
//       { seatInfoId: "26004559:26000084:001:522", seatGrade: "12",
//         seatGradeName: "1루 레드석", salesPrice: 19000, rowNo: "101블럭 8열",
//         seatNo: "76", posLeft: 1390.755, posTop: 686.112, isExposable: true,
//         seatGroupId: null, floor: "" },
//       ...
//     ],
//     bits: "111111111111...0..."   // '1'=예매가능, '0'=점유/불가 (seats 와 같은 순서)
//   }
// 등급 보강: 일부 공연은 seatMeta 의 seatGrade 가 null 이라 preselect 가 "좌석 요청이 잘못 되었습니다"(P40021)
//   로 거절된다. getRemainSeats 의 blockKey→seatGrade 매핑으로 채운다(블록에 등급이 하나일 때만).
const gradeMaps = { info: {}, byBlock: {} };   // info[grade] = {name, allocated}, byBlock[blockKey] = Set(grade)
const loadGradeMaps = async () => {
  try {
    const { list } = await fetchRemain();
    for (const x of list) {
      const g = String(x.seatGrade);
      gradeMaps.info[g] = { name: x.seatGradeName || "", allocated: x.allocatedSeatCount || 0, remain: x.remainSeatCount || 0 };
      for (const bk of (x.blockKey || [])) (gradeMaps.byBlock[bk] = gradeMaps.byBlock[bk] || new Set()).add(g);
    }
  } catch (e) { log("등급 매핑 조회 실패(무시):", String(e)); }
};
const fillGrades = (blk) => {
  const gs = gradeMaps.byBlock[blk.blockKey];
  const only = gs && gs.size === 1 ? [...gs][0] : null;
  for (const s of blk.seats) {
    if (s.seatGrade == null || s.seatGrade === "") {
      if (only) s.seatGrade = only;
    }
    if (!s.seatGradeName && gradeMaps.info[String(s.seatGrade)]) s.seatGradeName = gradeMaps.info[String(s.seatGrade)].name;
  }
  return blk;
};

const BLOCK_CHUNK = 11;   // 한 번에 조회할 최대 블록 수
const fetchBlock = async (blockKey) => (await fetchBlocks([blockKey]))[0];

// fetchBlocks(blockKeys) : BLOCK_CHUNK 개씩 나눠(청크는 병렬) fetchBlocksChunk 로 조회하고 입력 순서대로 합쳐 반환.
//   각 청크는 seatMeta 1회 + seatStatus 1회.
// fetchBlocksChunk(blockKeys) : 여러 블록을 seatMeta 1회 + seatStatus 1회로 한꺼번에 조회한다.
//   blockKeys=A&blockKeys=B... 로 반복 전달(프론트와 동일). seatStatus.data[i] 는 blockKeys[i] 의 비트맵.
//   반환: blockKeys 와 같은 순서의 fetchBlock() 결과 배열. 블록 정보가 없으면 seats=[] / bits="".
//   응답 개수가 어긋나면(status.data 길이 != 요청 수) 안전하게 블록별 개별 조회로 폴백한다.
const fetchBlocksChunk = async (blockKeys, _single) => {
  const base = `goodsCode=${goodsCode}&placeCode=${placeCode}&playSeq=${playSeq}`;
  const q = `${base}&${blockKeys.map((k) => "blockKeys=" + encodeURIComponent(k)).join("&")}&bizCode=${bizCode}`;
  const [meta, status] = await Promise.all([
    j(`/onestop/api/seatMeta?${q}`),
    j(`/onestop/api/seatStatus?${q}`),
  ]);
  // 응답 형태: [ {blockKey,seats} ] 또는 { data: [ {blockKey,seats} ] }
  const metaList = Array.isArray(meta.body) ? meta.body
    : (meta.body && Array.isArray(meta.body.data) ? meta.body.data : null);
  const statusList = status.body && Array.isArray(status.body.data) ? status.body.data : null;
  if (!metaList || !statusList || statusList.length !== blockKeys.length) {
    if (blockKeys.length > 1 && !_single) {
      log(`묶음 조회 응답 이상(meta=${meta.status} status=${status.status} ` +
          `statusLen=${statusList && statusList.length}/${blockKeys.length}) → 개별 조회로 폴백`);
      const out = [];
      for (const k of blockKeys) { out.push((await fetchBlocksChunk([k], true))[0]); await sleep(100); }
      return out;
    }
    log("seatMeta/seatStatus 이상 meta=" + meta.status + " status=" + status.status + " body=" +
        String(typeof meta.body === "string" ? meta.body : JSON.stringify(meta.body)).slice(0, 200));
  }
  return blockKeys.map((blockKey, i) => {
    const blk = metaList ? metaList.find((b) => b.blockKey === blockKey) : null;
    const bits = statusList && statusList[i] ? hexToBits(statusList[i]) : "";
    return fillGrades({ blockKey, seats: blk ? blk.seats : [], bits });
  });
};

// pickSeats(blk, grades, count, consecutive) : 비트맵으로 예매가능 좌석을 추려 조건에 맞게 고른다.
//   파라미터:
//     blk         = fetchBlock() 결과
//     grades      = ["12","14"]  (빈 배열이면 전 등급 허용)
//     count       = 2            (필요 매수)
//     consecutive = true         (같은 열 연속 seatNo 우선. 실패 시 아무 좌석으로 폴백)
//   반환:
//     성공 예) [ {seatInfoId,...,seatNo:"76"}, {seatInfoId,...,seatNo:"77"} ]  (좌석 count개 배열)
//     실패 예) null   (예매가능 좌석이 count 미만)
const pickSeats = (blk, grades, count, consecutive) => {
  const seats = blk.seats;
  const avail = [];
  for (let i = 0; i < seats.length; i++) {
    if (blk.bits[i] !== "1") continue;              // '1' = 예매가능
    const s = seats[i];
    if (!s.isExposable) continue;
    if (grades.length && !grades.includes(String(s.seatGrade))) continue;
    avail.push(s);
  }
  if (avail.length < count) return null;
  if (consecutive && count > 1) {
    const byRow = {};
    for (const s of avail) (byRow[s.rowNo] = byRow[s.rowNo] || []).push(s);
    for (const row of Object.values(byRow)) {
      row.sort((a, b) => (parseInt(a.seatNo, 10) || 0) - (parseInt(b.seatNo, 10) || 0));
      for (let i = 0; i + count <= row.length; i++) {
        const seg = row.slice(i, i + count);
        const nums = seg.map((s) => parseInt(s.seatNo, 10));
        let ok = true;
        for (let k = 1; k < nums.length; k++) if (nums[k] !== nums[k-1] + 1) { ok = false; break; }
        if (ok) return seg;
      }
    }
  }
  return avail.slice(0, count);
};

// pickAdjacent(blk, seedId, count) : 사용자가 첫 클릭한 좌석(seed)을 포함해 같은 열에서
//   좌우로 연속 seatNo 를 count 개까지 확장한다. seed 가 중앙에 가깝도록(좌우 균형) 창을 고른다.
//   seed 는 이미 내가 선점한 상태라 비트맵이 '0' 이어도 허용한다.
//   반환: 성공 시 seatNo 오름차순 좌석 배열(seed 포함), 불가능하면 null.
const pickAdjacent = (blk, seedId, count) => {
  const idx = blk.seats.findIndex((s) => s.seatInfoId === seedId);
  if (idx < 0) return null;
  const seed = blk.seats[idx];
  const usable = (i) => {
    const s = blk.seats[i];
    return i === idx || (blk.bits[i] === "1" && s.isExposable && String(s.seatGrade) === String(seed.seatGrade));
  };
  const byNo = new Map();
  blk.seats.forEach((s, i) => {
    if (s.rowNo === seed.rowNo && usable(i)) byNo.set(parseInt(s.seatNo, 10), s);
  });
  const sn = parseInt(seed.seatNo, 10);
  // 좌(−)/우(+) 로 연속 구간 길이 계산
  let lo = sn, hi = sn;
  while (byNo.has(lo - 1)) lo--;
  while (byNo.has(hi + 1)) hi++;
  if (hi - lo + 1 < count) return null;
  // 가능한 시작점 중 seed 가 가장 가운데에 오는 창
  let best = null, bestScore = Infinity;
  for (let st = Math.max(lo, sn - count + 1); st <= Math.min(sn, hi - count + 1); st++) {
    const score = Math.abs((st + (count - 1) / 2) - sn);
    if (score < bestScore) { bestScore = score; best = st; }
  }
  if (best === null) return null;
  const out = [];
  for (let n = best; n < best + count; n++) out.push(byNo.get(n));
  return out;
};

// pickByAisle(blk, grade, count, allocated) : 한 블록에서 해당 등급의 예매가능 연석 후보 중
//   '통로에 가장 가까운' 것을 고른다. 통로 = 열의 양 끝(전체 좌석 기준, 점유 좌석 포함).
//   - allocated > 1 (테이블석 등): 인원 고정 상품이라 allocated 석이 한 묶음.
//       seatGroupId 가 있으면 같은 그룹 전원이 예매가능일 때만 후보, 없으면 연속 allocated 석.
//   - 그 외: 같은 열 연속 count 석.
//   rowFilter(rowNo) : (선택) false 를 반환하는 열은 후보에서 제외.
//   반환: { seats, score, blk } (score 낮을수록 통로에 가까움) 또는 null.
const pickByAisle = (blk, grade, count, allocated, rowFilter) => {
  const g = String(grade);
  const need = allocated > 1 ? allocated : count;
  const rows = {};
  blk.seats.forEach((s, i) => {
    if (String(s.seatGrade) !== g) return;
    // 휠체어석(isWheelchair 는 호출 쪽 스코프에서 정의)은 좌석 단위로 항상 제외. 같은 등급 번호를 쓰거나
    // 등급이 null 이어도 걸러진다. 제외 좌석은 ok=false 라 연석 구간에도 끼지 못한다.
    const excluded = typeof isWheelchair === "function" && isWheelchair(s);
    (rows[s.rowNo] = rows[s.rowNo] || []).push({ s, ok: blk.bits[i] === "1" && !!s.isExposable && !excluded });
  });
  const num = (x) => parseInt(x.s.seatNo, 10);
  let best = null;
  const consider = (seats, score, tie) => {
    if (!best || score < best.score || (score === best.score && tie < best.tie)) best = { seats, score, tie, blk };
  };
  for (const row of Object.values(rows)) {
    row.sort((a, b) => (num(a) || 0) - (num(b) || 0));
    if (rowFilter && !rowFilter(row[0].s.rowNo)) continue;
    const n = row.length;
    const tie = row[0].s.rowIdx || 0;
    if (allocated > 1 && row.every((x) => x.s.seatGroupId)) {
      const groups = {};
      row.forEach((x, p) => (groups[x.s.seatGroupId] = groups[x.s.seatGroupId] || []).push({ ...x, p }));
      for (const m of Object.values(groups)) {
        if (m.length !== need || !m.every((x) => x.ok)) continue;
        const lo = Math.min(...m.map((x) => x.p)), hi = Math.max(...m.map((x) => x.p));
        consider(m.map((x) => x.s), Math.min(lo, n - 1 - hi), tie);
      }
      continue;
    }
    for (let p = 0; p + need <= n; p++) {
      const w = row.slice(p, p + need);
      if (!w.every((x) => x.ok)) continue;
      const nums = w.map(num);
      if (!nums.every(Number.isFinite)) continue;
      let seq = true;
      for (let k = 1; k < nums.length; k++) if (nums[k] !== nums[k - 1] + 1) { seq = false; break; }
      if (!seq) continue;
      consider(w.map((x) => x.s), Math.min(p, n - (p + need)), tie);
    }
  }
  return best;
};

const fetchBlocks = async (blockKeys) => {
  const chunks = [];
  for (let i = 0; i < blockKeys.length; i += BLOCK_CHUNK) chunks.push(blockKeys.slice(i, i + BLOCK_CHUNK));
  return (await Promise.all(chunks.map(fetchBlocksChunk))).flat();
};
