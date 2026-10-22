import { billUsage, buildStations, CONTRACT_TAG, dataCheckText, SEVERITY_TONE } from "../Detail/data.js";
import { compareText, dateValue, nextSortKey, sortRows, splitSortKey } from "../../sorting.js";
// 서울교통공사 노선색.
export const lineColors = {
  1: "#0052a4", 2: "#00a84d", 3: "#ef7c1c", 4: "#00a5de", 5: "#996cac",
  6: "#cd7c2f", 7: "#747f00", 8: "#e6186c",
};
export const riskMeta = {
  all: { label: "전체", tone: "all" }, alert: { label: "경고", tone: "alert" },
  warn: { label: "주의", tone: "warn" }, ok: { label: "정상", tone: "ok" },
  unknown: { label: "자료 확인", tone: "unknown" },
  analysis_pending: { label: "분석 대기", tone: "waiting" },
  pending: { label: "첫 수집 대기", tone: "pending" },
  billing_only: { label: "청구 전용", tone: "billing" },
};
export function buildDashboard(data) {
  const stations = buildStations(data.bills, data.stations, data.daily, data.risk, data.meters).flatMap((s) => s.lines.map((l) => {
    const reference = data.status?.reference_date || data.status?.latest_risk;
    const stale = Boolean(l.risk && reference && l.risk.기준일 < String(reference).slice(0, 10));
    const waiting = l.dataMode === "daily" && !l.risk && !l.riskError;
    const lastBill = l.bills.at(-1);
    return {
      id: l.meterId, stationId: s.id, name: s.역명, displayName: l.display_name || l.고객번호,
      dataMode: l.dataMode, latestBillMonth: lastBill?.ym || "", contractTag: CONTRACT_TAG[l.contractKind] || "",
      latestBillUsage: lastBill ? billUsage(lastBill) : null, latestBillAmount: lastBill?.납부금액 ?? null,
      office: l.영업사업소, officeId: String(l.office_id ?? ""), lines: [String(l.line)],
      risk: l.dataMode === "billing_only" ? "billing_only" : l.dataMode === "pending" ? "pending" : l.riskError || stale ? "unknown" : SEVERITY_TONE[l.risk?.severity] || "analysis_pending",
      riskDetail: l.dataMode !== "daily" ? "" : l.riskError ? dataCheckText(l.riskNote) : stale ? "분석일이 오래됨" : "",
      // 분석 대기인 까닭을 아는 경우에는 이름에 함께 적는다.
      riskLabel: waiting && l.historyShort ? "분석 대기(학습 데이터 부족)" : "",
      customerNo: l.고객번호, usage: l.daily?.at(-1)?.value ?? l.risk?.actual ?? null,
      delta: l.riskError ? null : l.risk?.pct ?? null,   // 자료 확인이면 지난 판정의 비율을 보이지 않는다
      dailyDate: l.daily?.at(-1)?.date || "", riskDate: l.risk?.기준일 || "", dailyEnabled: l.daily_enabled,
    };
  }));
  return { stations, offices: data.offices || [] };
}

// 계량기 목록 정렬 키: "<열>-asc|desc". 열: name(역명) · date(기준일) · usage(일 사용량) · delta(예측 대비) · amount(납부금액, 청구 확인만).
// 기준일은 일일 관제에서는 사용량 날짜(없으면 분석일), 청구 확인에서는 최근 청구월이다. 숫자·날짜 열은 처음 누르면 큰 값·최신이 먼저다.
export const defaultSort = "name-asc";
export const sortDefaults = { name: "asc", date: "desc", usage: "desc", delta: "desc", amount: "desc" };
export const nextSort = (current, column) => nextSortKey(current, column, sortDefaults);

// 실제 날짜로 비교할 수 있는 시각(ms). 비어 있거나 YYYY-MM(-DD) 형식이 아니면 null.
export function referenceTime(station, billingOnly = false) {
  return dateValue(billingOnly ? station.latestBillMonth : station.dailyDate || station.riskDate);
}

const byName = (a, b) => compareText(a.name, b.name) || compareText(a.displayName, b.displayName) || compareText(a.customerNo, b.customerNo);

// 원본 배열은 바꾸지 않는다. 역명이 같으면 계량기 이름·고객번호 순, 값이 없는 계량기는 어느 방향이든 뒤로 간다.
export function sortStations(stations, sortKey = defaultSort, billingOnly = false) {
  const columns = {
    name: { value: (s) => s.name, type: "text" },
    date: { value: (s) => (billingOnly ? s.latestBillMonth : s.dailyDate || s.riskDate), type: "date" },
    // 일 사용량·예측 대비는 일일 관제에만, 납부금액은 청구 확인에만 있는 열이다. 다른 보기에서 고른 열이면
    // 기본 정렬로 돌아간다(보기를 바꿔도 정렬 키는 남는다).
    ...(!billingOnly && { usage: { value: (s) => s.usage, type: "number" }, delta: { value: (s) => s.delta, type: "number" } }),
    ...(billingOnly && { amount: { value: (s) => s.latestBillAmount, type: "number" } }),
  };
  const { column } = splitSortKey(sortKey, defaultSort);
  const tieBreak = column === "name" ? (a, b) => compareText(a.displayName, b.displayName) || compareText(a.customerNo, b.customerNo) : byName;
  return sortRows(stations, columns[column] ? sortKey : defaultSort, columns, tieBreak);
}

// 기간 안의 자료만 남긴 /api/data 사본. 표·노선도는 이 사본으로 buildDashboard 를 다시 돌려, 일 사용량·위험도·기준일이
// 그 기간의 마지막 값이 된다. 위험도의 "분석일이 오래됨" 기준(reference_date)도 기간 안 마지막 분석일로 맞춘다.
export function clipToPeriod(data, start, end) {
  const inDay = (date) => Boolean(date) && date >= start && date <= end;
  const inMonth = (month) => Boolean(month) && month >= start.slice(0, 7) && month <= end.slice(0, 7);
  const daily = Object.fromEntries(Object.entries(data.daily || {}).map(([id, series]) => [id, {
    ...series, usage: (series.usage || []).filter((r) => inDay(r.date)), ridership: (series.ridership || []).filter((r) => inDay(r.date)),
  }]));
  const risk = Object.fromEntries(Object.entries(data.risk || {}).map(([id, rows]) => [id, (rows || []).filter((r) => inDay(r.date))]));
  const bills = Object.fromEntries(Object.entries(data.bills || {}).map(([id, entry]) => [id, {
    ...entry, bills: (entry.bills || []).filter((b) => inMonth(b.ym || String(b.id || "").split(":")[1])),
  }]));
  const latestRisk = Object.values(risk).flat().reduce((latest, r) => (r.date > latest ? r.date : latest), "");
  return { ...data, daily, risk, bills, status: { ...data.status, reference_date: latestRisk || null, latest_risk: latestRisk || null } };
}

// 기간을 골랐을 때 그 기간에 자료가 있는 계량기만. 청구 확인 보기(billing)와 청구 전용 계량기는 청구서,
// 일일 관제 보기는 사용량이나 분석 결과가 그 기간에 있어야 한다.
export const hasPeriodData = (station, billing = false) => (billing || station.dataMode === "billing_only" ? Boolean(station.latestBillMonth) : Boolean(station.dailyDate || station.riskDate));

// 계량기 줄에 보일 이름. 초기 자료의 계량기 이름은 "역명+호선번호"(동대문역1)라, 이름이 정확히 그 역명과 그 계량기 호선을
// 이은 것일 때만 역명으로 보여 준다(호선은 줄 앞 노선 배지로 보인다). "을지로3가역"의 숫자나 사용자가 붙인 이름은 그대로다.
export function meterName(meter) {
  const name = String(meter?.displayName ?? "");
  const station = String(meter?.name ?? "");
  const lines = (meter?.lines || []).filter(Boolean).map(String);
  if (station && lines.some((line) => name === `${station}${line}`)) return station;
  return name || station;
}

// 노선도 역명 라벨 자리(노선도 좌표). 기본은 노드 오른쪽이고, 오른쪽 라벨 자리에 다른 노드가 있을 때만 위쪽에 둔다.
// 라벨은 노드 오른쪽 28에서 시작하고 위아래로 약 16, 노드 점은 테두리까지 반지름 약 21이라 오른쪽으로 7~320,
// 위아래 37 안에 있는 노드만 걸린다. 바로 위아래로 붙은 역(삼각지역·신용산역)은 둘 다 오른쪽이라 서로의 점을 가리지 않는다.
export const labelSide = (point, points) => points.some((other) => other.id !== point.id
  && other.x - point.x > 7 && other.x - point.x < 320 && Math.abs(other.y - point.y) < 37) ? "above" : "right";

// billing: 청구 확인 보기. 위험도 색은 쓰지 않는다(risk는 모두 billing_only).
// daily: 일일 관제 계량기(첫 수집 대기 포함)가 하나라도 있는 역. 청구 확인 보기에서 남색으로 따로 그린다.
export function buildMapGroups(stations, locations = {}, billing = false) {
  const groups = new Map();
  const rank = { billing_only: -2, pending: -1, analysis_pending: 0, ok: 1, unknown: 2, warn: 3, alert: 4 };
  for (const station of stations) {
    const id = station.stationId || station.name;
    const position = locations[id] || locations[station.id] || locations[station.customerNo];
    if (!groups.has(id)) groups.set(id, { id, name: station.name, meters: [], risk: "billing_only", daily: false, position: null });
    const group = groups.get(id);
    group.meters.push(station);
    if (station.dataMode !== "billing_only") group.daily = true;
    if (position && Number.isFinite(position.x) && Number.isFinite(position.y) && position.x >= 0 && position.x <= 100 && position.y >= 0 && position.y <= 100) group.position = position;
    if (!billing && rank[station.risk] > rank[group.risk]) group.risk = station.risk;
  }
  return [...groups.values()];
}
