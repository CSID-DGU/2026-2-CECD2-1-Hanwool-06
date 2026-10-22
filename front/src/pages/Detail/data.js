const KDOW = ["일", "월", "화", "수", "목", "금", "토"];
export function kdate(iso) {
  if (!iso) return "기준일 없음";
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  const date = new Date(y, m - 1, d);
  return Number.isNaN(date.getTime()) ? "기준일 없음" : `${m}/${d} (${KDOW[date.getDay()]})`;
}
export const won = (n) => Number.isFinite(n) ? Math.round(n).toLocaleString("ko-KR") : "—";
export const ton = (n) => Number.isFinite(n) ? n.toLocaleString("ko-KR", { maximumFractionDigits: 1 }) : "—";
export const signedPct = (pct, missing = "—") => pct == null ? missing : `${pct > 0 ? "+" : ""}${pct}%`;
export const SEVERITY_TONE = { 정상: "ok", 주의: "warn", 경고: "alert" };
// 차트 계열색: 사용량은 공사 청색, 승하차는 청록, 요금은 황동색.
export const CHART_COLORS = { usage: "#17288b", riders: "#0e7c7b", fee: "#8a6d3b" };
const baseName = (name) => String(name || "").replace(/\d+$/, "");
const sorted = (series = []) => [...series].sort((a, b) => a.date.localeCompare(b.date));

// 자료 확인의 이유 한 줄. note: 위험도 행(또는 업무 요약 항목)의 reason·days.
export function dataCheckText(note) {
  if (note?.reason === "값 그대로") return `원격검침 값 ${note.days}일째 그대로`;
  if (note?.reason === "다시 움직인 첫날") return "원격검침 값 다시 움직인 첫날";
  return "최근 관측값 오류";
}

// 청구서로 본 계약 종류. 고지서에 적힌 것만으로 가르고, 확인되지 않는 것은 말하지 않는다.
// - "water": 수돗물 사용량이나 상수도 요금(기본료·합계 포함)·물이용부담금이 한 번이라도 있다.
// - "groundwater": 그런 것이 전혀 없고, 하수도 사용료가 나온 고지서에 지하수 사용량이 적혀 있다(적힌 것은 모두 0보다 크다).
// - "sewer": 수돗물 흔적이 전혀 없고 하수도 사용료가 나온 적은 있지만, 지하수 사용량이 적힌 고지서는 없다.
// - "none": 사용량도 금액도 전혀 없다. 내역 없이 금액만 있거나 청구서가 없으면 알 수 없어 "".
export function contractKind(bills = []) {
  if (!bills.length) return "";
  const n = (v) => Number(v) || 0;
  const tap = ["사용량", "총사용량", "상수도_사용료", "상수도_기본료", "상수도_합계", "물이용부담금"];
  if (bills.some((b) => tap.some((key) => n(b[key]) > 0))) return "water";
  const sewer = bills.filter((b) => n(b.하수도_사용료) > 0);
  if (!sewer.length) return bills.some((b) => n(b.납부금액) > 0 || n(b.부과금액) > 0 || n(b.총사용금액) > 0) ? "" : "none";
  const groundwater = sewer.map((b) => b.지하수사용량 ?? b.지하수_사용량).filter((v) => v != null);
  return groundwater.length && groundwater.every((v) => n(v) > 0) ? "groundwater" : "sewer";
}
// 수돗물 계약이 아닐 때 이름 옆에 붙이는 꼬리표와 설명.
export const CONTRACT_TAG = { groundwater: "하수도(지하수)", sewer: "수돗물 사용 없음", none: "사용 기록 없음" };
export const CONTRACT_NOTE = {
  groundwater: "수돗물 사용량이 없고, 하수도 사용료가 지하수 사용량으로 청구되는 계약(고지서에 지하수 사용량이 적혀 있음)",
  sewer: "수돗물 사용량이 한 번도 없는 계약(하수도 사용료가 청구된 적은 있지만 고지서에 지하수 사용량은 없음)",
  none: "청구서에 사용량도 금액도 없는 계약",
};

// 자료 확인 행의 이유(없으면 null). 판정 행에는 reason이 없다.
const noteOf = (r) => (r?.err ? { reason: r.reason || "", since: r.since, days: r.days, usual: r.usual, date: r.date, actual: r.actual } : null);
const riskPct = (r) => r.pred > 0 ? Math.round((r.residual / r.pred) * 100) : null;
const riskDay = (r) => ({
  date: r.date, err: Boolean(r.err), severity: r.err ? "자료 확인" : r.severity,
  predicted: r.pred, actual: r.actual, error: r.residual, pct: r.err ? null : riskPct(r),
  방향: r.dir || (r.residual >= 0 ? "과다" : "과소"), note: noteOf(r),
});

export function buildRisk(arr = []) {
  const all = sorted(arr);
  const clean = all.filter((r) => !r.err && Number.isFinite(r.pred) && Number.isFinite(r.actual) && Number.isFinite(r.residual));
  if (!clean.length) return null;
  const last = clean.at(-1);
  return {
    predicted: last.pred, actual: last.actual, error: last.residual, pct: riskPct(last),
    방향: last.dir || (last.residual >= 0 ? "과다" : "과소"), severity: last.severity,
    기준일: last.date, latestError: Boolean(all.at(-1)?.err),
    // 판정된 모든 날. 관측 오류가 있던 날은 '자료 확인'으로 둔다.
    days: all.map(riskDay),
  };
}

// 달력 한 달치 칸. 일요일 시작이라 1일 앞은 빈칸(null)이고, 판정이 없는 날은 severity 가 없다.
export function monthCells(ym, days = []) {
  const [year, month] = ym.split("-").map(Number);
  const byDate = new Map(days.map((d) => [d.date, d]));
  const cells = Array.from({ length: new Date(year, month - 1, 1).getDay() }, () => null);
  for (let day = 1, count = new Date(year, month, 0).getDate(); day <= count; day += 1) {
    const date = `${ym}-${String(day).padStart(2, "0")}`;
    cells.push({ date, day, ...(byDate.get(date) || {}) });
  }
  return cells;
}
export const monthOf = (iso) => String(iso).slice(0, 7);
export function shiftMonth(ym, delta) {
  const [year, month] = ym.split("-").map(Number);
  const moved = new Date(year, month - 1 + delta, 1);
  return `${moved.getFullYear()}-${String(moved.getMonth() + 1).padStart(2, "0")}`;
}

// Every registered contract is retained, even before the first bill or collection.
export function buildStations(bills = {}, meta = {}, daily = {}, risk = {}, meters = []) {
  const registry = meters.length ? meters : Object.entries(meta).map(([cid, m]) => ({
    id: cid, customer_number: cid, station_id: m.station_id || baseName(m.역명),
    station_name: baseName(m.역명), line: m.호선, office_name: m.영업사업소, active: true,
  }));
  const groups = new Map();
  for (const meter of registry) {
    if (meter.deleted_at || meter.active === false || meter.active === 0) continue;
    const cid = meter.customer_number;
    const key = String(meter.id);
    const b = bills[key] || {}, m = meta[key] || {}, d = daily[key] || {};
    const stationId = meter.station_id || m.station_id || baseName(m.역명 || meter.station_name);
    const name = meter.station_name || baseName(m.역명 || b.역명) || "미지정 역";
    const usage = d.usage?.filter((p) => Number.isFinite(p.value)) || [];
    const dataMode = meter.data_mode || m.data_mode || (usage.length ? "daily" : meter.daily_enabled ? "pending" : "billing_only");
    const riskRows = dataMode === "daily" ? sorted(risk[key]) : [];
    const verdict = buildRisk(riskRows);
    const record = {
      ...meter, meterId: String(meter.id), line: meter.line ?? m.호선 ?? null,
      dataMode,
      고객번호: cid, 영업사업소: meter.office_name || m.영업사업소 || b.사업소명 || "미지정",
      base: name, 주소: meter.address || b.주소 || "—", 용도: meter.purpose || b.용도 || "—", 사용자명: name,
      bills: [...(b.bills || [])].sort((a, b) => a.ym.localeCompare(b.ym)), contractKind: contractKind(b.bills),
      전자수용가번호: b.전자수용가번호, 전자납부번호: b.전자납부번호,
      계량기번호: meter.meter_number || b.계량기번호, 구경: b.구경, 가구수: b.가구수,
      daily: usage.length ? sorted(usage) : null,
      ridership: d.ridership?.length ? sorted(d.ridership) : null,
      risk: verdict,
      riskDays: verdict?.days || riskRows.map(riskDay),   // 정상 판정이 한 번도 없어도 자료 확인 날짜는 남긴다.
      historyShort: Boolean(m.history_short),   // 학습 이력이 모자라 모델이 판정하지 않은 계량기
      riskError: Boolean(riskRows.at(-1)?.err),
      riskNote: noteOf(riskRows.at(-1)),   // 마지막 판정일이 자료 확인이면 그 이유
    };
    if (!groups.has(stationId)) groups.set(stationId, { id: stationId, 역명: name, 사용자명: name, lines: [] });
    groups.get(stationId).lines.push(record);
  }
  return [...groups.values()].map((station) => ({
    ...station,
    lines: station.lines.sort((a, b) => Number(a.line) - Number(b.line) || a.고객번호.localeCompare(b.고객번호)),
  })).sort((a, b) => a.역명.localeCompare(b.역명, "ko"));
}
export function latestDate(stations) {
  return stations.flatMap((s) => s.lines).flatMap((l) => [l.daily?.at(-1)?.date, l.ridership?.at(-1)?.date, l.risk?.기준일]).filter(Boolean).sort().at(-1) || "";
}
export function isDetailDate(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const date = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(date.getTime()) && date.toISOString().slice(0, 10) === value;
}
export const detailHref = (meterId, date) => `#/detail?meter=${encodeURIComponent(meterId)}${isDetailDate(date) ? `&date=${date}` : ""}`;
export const billNoticeNumber = (bill) => String(bill.notice_number || bill.고지번호 || "").trim();
export const billLabel = (bill) => `${bill.gubun || "정기분"}${billNoticeNumber(bill) ? ` (고지번호 ${billNoticeNumber(bill)})` : ""}`;
export function billUsage(bill) {
  if (bill.사용량 != null) return bill.사용량;
  const detailed = !bill.summary_only && (Object.hasOwn(bill, "사용량") || bill.detail_available || bill.source === "i121_public_detail");
  return detailed ? null : bill.총사용량 ?? null;
}
export const billGroundwater = (bill) => ({
  usage: bill.지하수사용량 ?? bill.지하수_사용량 ?? null,
  current: bill.지하수당월지침 ?? bill.지하수_당월지침 ?? null,
  previous: bill.지하수전월지침 ?? bill.지하수_전월지침 ?? null,
});
export function billWindow(bills, ym) {
  const [year, month] = ym.split("-").map(Number);
  const selected = year * 12 + month;
  return bills.filter((b) => {
    const [y, m] = b.ym.split("-").map(Number);
    return y * 12 + m <= selected && y * 12 + m > selected - 12;
  });
}

// 12px 축 글씨 기준 라벨 폭(px). 한글·전각은 글자 하나가 한 칸, 숫자·기호는 그보다 좁다.
export function labelWidth(label) {
  return [...String(label ?? "")].reduce((width, ch) => width + (ch.charCodeAt(0) > 0x7f ? 12 : 6.8), 0);
}

// 축 라벨을 몇 개마다 하나씩 찍을지. 가장 긴 라벨이 점 사이 간격(slot)에 여유(gap)를 두고 들어가야 겹치지 않는다.
// minStep 은 호출한 쪽이 원하는 최소 간격(labelEvery). 라벨은 마지막 점에서 거꾸로 step 마다 찍는다(xLabelVisible).
export function xLabelStep(labels, slot, minStep = 1, gap = 12) {
  const widest = Math.max(0, ...labels.map(labelWidth));
  const needed = slot > 0 ? Math.ceil((widest + gap) / slot) : labels.length;
  return Math.max(1, minStep || 1, needed);
}
export const xLabelVisible = (index, count, step) => (count - 1 - index) % step === 0;

export function chartRange(values, showValues = false) {
  const finite = values.filter(Number.isFinite);
  if (!finite.length) return null;
  const min = Math.min(...finite), max = Math.max(...finite);
  const span = max - min || 1;
  const lo = min >= 0 ? Math.max(0, min - span * 0.14) : min - span * 0.14;
  // Three integer labels must remain distinct, including a series of valid zeroes.
  const hi = Math.max(lo + 2, max + span * (showValues ? 0.2 : 0.14));
  return { lo, hi };
}

// y축 눈금(위·가운데·아래) 숫자 중 가장 긴 글자 수. 자료가 없으면 0.
// 나란히 놓은 그래프들이 같은 값을 LineChart labelChars로 받으면 왼쪽 여백, 곧 x축 길이가 같아진다.
export function yLabelChars(values, formatY = (v) => v, showValues = false) {
  const range = chartRange(values, showValues);
  if (!range) return 0;
  const { lo, hi } = range;
  return Math.max(...[hi, (lo + hi) / 2, lo].map((t) => String(formatY(Math.round(t))).length));
}
