import { useEffect, useMemo, useState } from "react";
import { exportUrl, request } from "../api.js";
import { ton, won, yLabelChars, CHART_COLORS } from "./Detail/data.js";
import { Card, SectionTitle } from "./Detail/ui.jsx";
import LineChart from "./Detail/LineChart.jsx";
import FileLink from "../components/FileLink.jsx";
import Dropdown, { SelectField } from "../components/Dropdown.jsx";
import { FilterHeader, SortHeader } from "../components/TableHeader.jsx";
import { collectedRange, compactWonFormatter, statisticsPeriod } from "./statisticsData.js";
import { compareText, nextSortKey, sortRows } from "../sorting.js";
import { exportFilename, exportRange } from "../exportNames.js";

// 월별 내역 표의 정렬 열과 기본 방향. 월은 시간순, 수치는 큰 값이 먼저.
const TABLE_SORT_DEFAULTS = { month: "asc", usage_ton: "desc", billed_usage_ton: "desc", billed_won: "desc" };
const TABLE_COLUMNS = {
  month: { value: (r) => r.month, type: "date" },
  usage_ton: { value: (r) => r.usage_ton, type: "number" },
  billed_usage_ton: { value: (r) => r.billed_usage_ton, type: "number" },
  billed_won: { value: (r) => r.billed_won, type: "number" },
};
const NUMBER_HINTS = { asc: "작은 값부터, 누르면 큰 값부터", desc: "큰 값부터, 누르면 작은 값부터", idle: "누르면 큰 값부터" };
const MONTH_HINTS = { asc: "오래된 달부터, 누르면 최신 달부터", desc: "최신 달부터, 누르면 오래된 달부터", idle: "누르면 최신 달부터" };
const rowOrder = (a, b) => compareText(a.month, b.month) || compareText(a.office_name, b.office_name) || compareText(a.line, b.line);

export function meterOption(meter) {
  return { value: String(meter.id), keywords: `${meter.customer_number} ${meter.station_name} ${meter.display_name || ""}`,
           label: `${meter.station_name} ${meter.line ? `${meter.line}호선 ` : ""}${meter.display_name || "기본 계량기"} (${meter.customer_number})` };
}

// 기본 기간은 그해 1월 1일(첫 자료일이 더 늦으면 그날)부터 마지막 수집일(없으면 오늘)까지.
const defaultFilters = (collected) => ({ ...statisticsPeriod(new Date(), collected?.last, collected?.first), office_id: "", line: "", meter_id: "" });
const sameFilters = (a, b) => ["start", "end", "office_id", "line", "meter_id"].every((key) => String(a[key] ?? "") === String(b[key] ?? ""));

export default function StatisticsPage({ data }) {
  // 자료가 있는 첫날·마지막 날. 시작일은 첫날부터, 종료일은 마지막 날(오늘을 넘지 않음)까지만 고를 수 있다.
  const collected = useMemo(() => collectedRange(data), [data]);
  const startLimit = collected?.first || undefined;
  const endLimit = statisticsPeriod(new Date(), collected?.last).end;
  // 처음 상태(조건 초기화의 기준). 화면을 열어 둔 사이 자료가 갱신돼도 바뀌지 않고, 초기화할 때 새로 정한다.
  const [baseline, setBaseline] = useState(() => defaultFilters(collected));
  const [filters, setFilters] = useState(baseline);
  const [query, setQuery] = useState(baseline);
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  // 표 안에서만 쓰는 정렬·조건. 조회 조건(query)과 Excel 내려받기에는 영향을 주지 않는다.
  const [sortKey, setSortKey] = useState("month-asc");
  const [tableOffice, setTableOffice] = useState("all");
  const [tableLine, setTableLine] = useState("all");
  const change = (key, value) => setFilters((f) => ({ ...f, [key]: value, ...(["office_id", "line"].includes(key) ? { meter_id: "" } : {}) }));
  useEffect(() => {
    let alive = true;
    setLoading(true); setError("");
    const params = new URLSearchParams(Object.entries(query).filter(([, value]) => value));
    request(`/stats?${params}`).then((value) => { if (alive) { setResult(value); setTableOffice("all"); setTableLine("all"); } })
      .catch((e) => { if (alive) { setError(e.message); setResult(null); } })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [query, data]);
  const rows = result?.rows || [];
  const queriedMeters = (data.meters || []).filter((m) => (!query.office_id || String(m.office_id) === query.office_id) && (!query.line || String(m.line) === query.line) && (!query.meter_id || String(m.id) === query.meter_id));
  const billingOnly = queriedMeters.length > 0 && queriedMeters.every((m) => m.data_mode === "billing_only");
  const months = [...new Set(rows.map((r) => r.month))].sort();
  const monthLabels = months.map((m) => m.slice(2).replace("-", "/"));   // "2026-03" → "26/03": 역 상세 청구 차트와 같은 표기
  const sum = (field, records = rows) => {
    const values = records.map((r) => r[field]).filter(Number.isFinite);
    return values.length ? values.reduce((a, b) => a + b, 0) : null;
  };
  const usageByMonth = months.map((m) => sum("usage_ton", rows.filter((r) => r.month === m)));
  const feeByMonth = months.map((m) => sum("billed_won", rows.filter((r) => r.month === m)));
  // 두 그래프를 나란히 비교하므로 y축 숫자 칸을 둘 중 긴 쪽에 맞춰, x축(월 간격) 길이를 같게 한다.
  // 요금 축은 억·만 단위로 짧게 써서 요금 그래프의 x축을 늘리고, 두 그래프의 y축 숫자 칸을 긴 쪽에 맞춰 x축 길이를 같게 한다.
  const feeFormat = compactWonFormatter(feeByMonth);
  const labelChars = billingOnly ? 0 : Math.max(yLabelChars(usageByMonth, ton), yLabelChars(feeByMonth, feeFormat));
  const meters = (data.meters || []).filter((m) => (!filters.office_id || String(m.office_id) === filters.office_id) && (!filters.line || String(m.line) === filters.line));
  const meterOptions = [{ value: "", label: "전체" }, ...meters.map(meterOption)];
  const summaryBills = sum("summary_bill_count");
  // 표 머리글의 조건(사업소·호선)과 정렬. 선택지는 조회 결과에 실제로 있는 값만.
  const officeItems = [...new Map(rows.map((r) => [String(r.office_id), r.office_name])).entries()].sort((a, b) => compareText(a[1], b[1])).map(([value, label]) => ({ value, label }));
  const lineItems = [...new Set(rows.map((r) => r.line).filter(Boolean))].sort((a, b) => Number(a) - Number(b)).map((line) => ({ value: String(line), label: `${line}호선` }));
  const visibleRows = sortRows(rows.filter((r) => (tableOffice === "all" || String(r.office_id) === tableOffice) && (tableLine === "all" || String(r.line) === tableLine)), sortKey, TABLE_COLUMNS, rowOrder);
  const tableFiltering = tableOffice !== "all" || tableLine !== "all";
  // 내려받기는 마지막으로 조회한 조건(query)에 월별 내역 머리글에서 고른 사업소·호선을 더한 것이다(화면에 보이는 행과 같은 범위).
  // 머리글 선택지는 조회 결과에 있는 값만이라 조회 조건보다 넓어지지 않는다.
  const exportQuery = { ...query, ...(tableOffice !== "all" ? { office_id: tableOffice } : {}), ...(tableLine !== "all" ? { line: tableLine } : {}) };
  // 파일명에는 그 조건과 파일에 실제로 들어가는 자료의 첫날·마지막 날을 쓴다.
  // 계량기를 골랐으면 사업소·호선은 그 계량기에 담겨 있으므로 "호선_역명_고객번호"만 쓴다(같은 역·호선에 계량기가 여럿일 수 있다).
  const queriedMeter = exportQuery.meter_id ? (data.meters || []).find((m) => String(m.id) === String(exportQuery.meter_id)) : null;
  const exportParts = queriedMeter
    ? [queriedMeter.line && `${queriedMeter.line}호선`, queriedMeter.station_name, queriedMeter.customer_number]
    : [exportQuery.office_id && ((data.offices || []).find((o) => String(o.id) === String(exportQuery.office_id))?.name || ""), exportQuery.line && `${exportQuery.line}호선`];
  const usageFile = exportFilename("usage", exportParts, exportRange(data, "usage", exportQuery));
  const billsFile = exportFilename("bills", exportParts, exportRange(data, "bills", exportQuery));
  // 입력란을 바꾸고 아직 조회하지 않았으면, 화면 결과와 내려받기는 이전 조회 조건 그대로라는 것을 알린다.
  const pendingQuery = ["start", "end", "office_id", "line", "meter_id"].some((key) => String(filters[key] ?? "") !== String(query[key] ?? ""));
  // 초기화 버튼은 상단 조회 줄에 하나만 둔다. 누르면 표 머리글의 조건·정렬도 함께 처음 상태로 돌아간다.
  const resetTable = () => { setTableOffice("all"); setTableLine("all"); setSortKey("month-asc"); };
  // 조회 조건(기간·사업소·호선·계량기)과 표 조건·정렬을 처음 상태로 되돌리고 바로 다시 조회한다.
  const conditioned = !sameFilters(filters, baseline) || !sameFilters(query, baseline) || tableFiltering || sortKey !== "month-asc";
  const resetAll = () => { const next = defaultFilters(collected); setBaseline(next); setFilters(next); setQuery(next); setError(""); resetTable(); };
  return <main className="management-page statistics-page">
    <div className="management-heading"><h1>사용량·요금 통계</h1></div>
    <form className="stats-filters" onSubmit={(e) => { e.preventDefault(); if (filters.start > filters.end) { setError("시작일은 종료일보다 늦을 수 없습니다."); return; } setQuery({ ...filters }); }}>
      <label className="form-field">시작일<input type="date" required value={filters.start} min={startLimit} max={filters.end} onChange={(e) => change("start", e.target.value)} /></label>
      <label className="form-field">종료일<input type="date" required value={filters.end} min={filters.start} max={endLimit} onChange={(e) => change("end", e.target.value)} /></label>
      <SelectField label="사업소" value={String(filters.office_id ?? "")} options={[{ value: "", label: "전체" }, ...(data.offices || []).map((o) => ({ value: String(o.id), label: o.name }))]} onChange={(value) => change("office_id", value)} />
      <SelectField label="호선" value={String(filters.line ?? "")} options={[{ value: "", label: "전체" }, ...[1,2,3,4,5,6,7,8].map((l) => ({ value: String(l), label: `${l}호선` }))]} onChange={(value) => change("line", value)} />
      <div className="form-field stats-meter"><span>계량기</span>
        <Dropdown label="계량기" value={filters.meter_id} options={meterOptions} onChange={(next) => change("meter_id", next)} buttonClassName="dd-field"
          searchable searchPlaceholder="역명, 계량기 이름, 고객번호 검색" emptyText="일치하는 계량기가 없습니다." />
      </div>
      <button className="primary-button" disabled={loading}>{loading ? "조회 중…" : "조회"}</button>
      {/* 조건이 없을 때도 자리는 남겨 둔다: 버튼이 나타났다 사라질 때 입력칸 폭이 흔들리지 않게 */}
      <button type="button" className={`text-button stats-reset ${conditioned ? "" : "is-idle"}`} onClick={resetAll} disabled={!conditioned}>조건 초기화</button>
    </form>
    {error && <p className="form-error" role="alert">{error}</p>}
    <div aria-live="polite" aria-busy={loading}>
      {!loading && result && <>
        <dl className="stats-totals">
          {!billingOnly && <div><dt>일일 사용량 합계</dt><dd>{ton(sum("usage_ton"))} 톤</dd><dd className="sub">검침 {won(sum("observation_count"))}건</dd></div>}
          <div><dt>청구 사용량 합계</dt><dd>{ton(sum("billed_usage_ton"))} 톤</dd><dd className="sub">청구서 {won(sum("bill_count"))}건</dd></div>
          <div><dt>청구 요금 합계</dt><dd>{won(sum("billed_won"))} 원</dd><dd className="sub">{summaryBills ? `상세 내역이 없는 청구서 ${won(summaryBills)}건 포함` : "청구월 기준"}</dd></div>
        </dl>
        {result.note && <p className="form-hint">{result.note}</p>}
        <div className={`stats-charts ${billingOnly ? "stats-charts--billing" : ""}`}>
          {!billingOnly && <Card title="월별 일일 사용량"><LineChart series={[{ color: CHART_COLORS.usage, values: usageByMonth }]} xLabels={monthLabels} yUnit="톤" xUnit="연/월" formatY={ton} height={220} labelChars={labelChars} /></Card>}
          <Card title="월별 청구 요금"><LineChart series={[{ color: CHART_COLORS.fee, values: feeByMonth }]} xLabels={monthLabels} yUnit="원" xUnit="연/월" formatY={feeFormat} height={220} labelChars={labelChars} /></Card>
        </div>
        <section className="table-panel">
          <SectionTitle as="h2" right={<>
            {billingOnly && <span className="export-note">청구 전용 계량기라 일일 사용량 자료가 없습니다</span>}
            {!billingOnly && <FileLink href={exportUrl({ ...exportQuery, kind: "usage" })} filename={usageFile} title={usageFile} saveAs>사용량 Excel</FileLink>}
            <FileLink href={exportUrl({ ...exportQuery, kind: "bills" })} filename={billsFile} title={billsFile} saveAs>청구내역 Excel</FileLink>
          </>}>월별 내역 <span className="count">{tableFiltering ? `${visibleRows.length}행 (전체 ${rows.length}행 중)` : `${rows.length}행`}</span></SectionTitle>
          <p className="form-hint stats-export-hint">Excel은 조회한 조건({query.start} ~ {query.end}) 그대로 내려받습니다.{tableFiltering ? " 표 머리글에서 고른 사업소·호선도 함께 반영됩니다." : ""}{pendingQuery ? " 바꾼 조건은 조회를 눌러야 결과와 내려받기에 반영됩니다." : ""}</p>
          <div className="table-wrap table-wrap--fixed"><table className="data-table data-table--controls"><caption className="sr-only">사업소·호선별 월별 통계</caption>
            <thead><tr>
              <SortHeader column="month" label="월" sortKey={sortKey} onSort={(c) => setSortKey(nextSortKey(sortKey, c, TABLE_SORT_DEFAULTS))} hints={MONTH_HINTS} />
              <FilterHeader label="사업소" value={tableOffice} onChange={setTableOffice} items={officeItems} />
              <FilterHeader label="호선" value={tableLine} onChange={setTableLine} items={lineItems} />
              {!billingOnly && <SortHeader column="usage_ton" label="일일 사용량 (톤)" sortKey={sortKey} onSort={(c) => setSortKey(nextSortKey(sortKey, c, TABLE_SORT_DEFAULTS))} className="num" hints={NUMBER_HINTS} />}
              <SortHeader column="billed_usage_ton" label="청구 사용량 (톤)" sortKey={sortKey} onSort={(c) => setSortKey(nextSortKey(sortKey, c, TABLE_SORT_DEFAULTS))} className="num" hints={NUMBER_HINTS} />
              <SortHeader column="billed_won" label="청구 요금 (원)" sortKey={sortKey} onSort={(c) => setSortKey(nextSortKey(sortKey, c, TABLE_SORT_DEFAULTS))} className="num" hints={NUMBER_HINTS} />
              <th className="num">{billingOnly ? "청구 건수" : "검침 / 청구 건수"}</th>
            </tr></thead>
            <tbody>{visibleRows.map((r) => <tr key={`${r.month}-${r.office_id}-${r.line}`}><td>{r.month}</td><td>{r.office_name}</td><td>{r.line ? `${r.line}호선` : "—"}</td>{!billingOnly && <td className="num">{ton(r.usage_ton)}</td>}<td className="num">{ton(r.billed_usage_ton)}</td><td className="num">{won(r.billed_won)}</td><td className="num">{billingOnly ? r.bill_count : `${r.observation_count} / ${r.bill_count}`}</td></tr>)}</tbody></table>
            {!rows.length && <p className="empty-state">선택한 기간에 자료가 없습니다.</p>}
            {rows.length > 0 && !visibleRows.length && <p className="empty-state">조건에 맞는 행이 없습니다. 상단의 조건 초기화를 누르면 처음 상태로 돌아갑니다.</p>}
          </div>
        </section>
      </>}
    </div>
  </main>;
}
