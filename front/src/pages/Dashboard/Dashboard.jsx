import { useMemo, useState } from "react";
import { buildDashboard, clipToPeriod, defaultSort, hasPeriodData, riskMeta, sortStations } from "./dashboardData.js";
import MapPanel from "./components/MapPanel.jsx";
import PageHeader from "./components/PageHeader.jsx";
import StationRow from "./components/StationRow.jsx";
import StationTable from "./components/StationTable.jsx";
import { EmptyNote, SectionTitle } from "../Detail/ui.jsx";
import { exportUrl } from "../../api.js";
import FileLink from "../../components/FileLink.jsx";
import { exportFilename, exportRange } from "../../exportNames.js";
import { statisticsPeriod } from "../statisticsData.js";
import CollectionPanel from "../../components/CollectionPanel.jsx";

export default function Dashboard({ data, collection }) {
  const [searchTerm, setSearchTerm] = useState("");
  const [selectedLine, setSelectedLine] = useState("all");
  const [selectedOffice, setSelectedOffice] = useState("all");
  const [selectedRisk, setSelectedRisk] = useState("all");
  const [view, setView] = useState("daily");
  const [sortKey, setSortKey] = useState(defaultSort);
  const [period, setPeriod] = useState({ start: "", end: "" });   // 빈 값이면 전체 기간(가장 오래된 자료일~오늘)
  // 맨 위 현황 문장은 기간과 상관없는 오늘 기준 현황이다.
  const { stations: allStations, offices } = useMemo(() => buildDashboard(data), [data]);
  const monitored = allStations.filter((s) => s.dataMode !== "billing_only");
  // 기간: 표·노선도·우선 확인 대상·내려받기가 함께 따른다. 기본값은 전체 기간이라 고르지 않으면 이전과 같다.
  const today = statisticsPeriod().end;
  const earliest = useMemo(() => {
    const usage = exportRange(data, "usage")?.first, bills = exportRange(data, "bills")?.first;
    return [usage, bills && `${bills}-01`].filter(Boolean).sort()[0] || today;
  }, [data, today]);
  const periodStart = period.start || earliest, periodEnd = period.end || today;
  const periodActive = Boolean(period.start || period.end);
  const periodError = periodStart > periodEnd ? "시작일이 종료일보다 늦습니다." : "";
  const stations = useMemo(() => (periodActive && !periodError ? buildDashboard(clipToPeriod(data, periodStart, periodEnd)).stations : allStations),
    [data, allStations, periodActive, periodError, periodStart, periodEnd]);
  // 기간을 골랐으면 그 기간에 자료가 있는 계량기만: 일일 관제는 사용량·분석, 청구 확인은 청구서 기준.
  const inPeriod = (station, billing = false) => !periodActive || periodError || hasPeriodData(station, billing);
  const term = searchTerm.trim().toLowerCase();
  // 검색어·호선·사업소·위험도 조건은 계량기 목록 머리글에서 바꾸고, 노선도와 우선 확인 대상도 같은 조건을 따른다.
  const scopedStations = stations.filter((station) =>
    (!term || `${station.name} ${station.customerNo} ${station.displayName} ${station.office}`.toLowerCase().includes(term))
    && (selectedLine === "all" || station.lines.includes(selectedLine))
    && (selectedOffice === "all" || station.officeId === selectedOffice)
  );
  const dailyStations = scopedStations.filter((s) => s.dataMode !== "billing_only" && inPeriod(s));
  const billingStations = scopedStations.filter((s) => inPeriod(s, true));
  // billingOnly는 청구 확인 보기다. 청구 전용 계량기뿐 아니라 일일 관제 계량기까지 모든 계량기의 최근 청구를 본다.
  const billingOnly = view === "billing";
  const viewStations = billingOnly ? billingStations : dailyStations;
  const filteredStations = viewStations.filter((station) => billingOnly || selectedRisk === "all" || station.risk === selectedRisk);
  const alerts = filteredStations.filter((s) => s.risk === "alert");
  const warnings = filteredStations.filter((s) => s.risk === "warn");
  const unknowns = filteredStations.filter((s) => s.risk === "unknown");
  // 표는 조건을 적용한 뒤 정렬한 사본을 받는다. 원본 배열과 노선도·우선 확인 대상의 순서는 그대로다.
  const sortedStations = sortStations(filteredStations, sortKey, billingOnly);
  const filtering = periodActive || Boolean(term) || selectedLine !== "all" || selectedOffice !== "all" || (!billingOnly && selectedRisk !== "all");
  const resetFilters = () => { setSearchTerm(""); setSelectedLine("all"); setSelectedOffice("all"); setSelectedRisk("all"); setSortKey(defaultSort); setPeriod({ start: "", end: "" }); };
  const switchView = (next) => { setView(next); setSelectedRisk("all"); };
  // 내려받기: 기간·사업소·호선을 서버로 보내고, 검색어가 있으면 검색 결과 계량기(meter_ids)만 받는다. 위험도는 보내지 않는다.
  // 파일명은 그 조건과 파일에 실제로 들어가는 자료의 첫날·마지막 날로 만든다.
  const searchIds = term ? scopedStations.map((s) => s.id) : null;
  const exportParams = { office_id: selectedOffice, line: selectedLine, start: periodStart, end: periodEnd, ...(searchIds ? { meter_ids: searchIds.join(",") } : {}) };
  const exportParts = [selectedOffice !== "all" && (offices.find((o) => String(o.id) === selectedOffice)?.name || ""), selectedLine !== "all" && `${selectedLine}호선`,
    term && `검색 ${searchTerm.trim()}`];
  const usageFile = exportFilename("usage", exportParts, exportRange(data, "usage", exportParams));
  const billsFile = exportFilename("bills", exportParts, exportRange(data, "bills", exportParams));
  const exportBlocked = periodError || (searchIds && !searchIds.length ? "검색 결과가 없어 내려받을 계량기가 없습니다." : "");
  const exportNote = exportBlocked || `${periodActive ? "고른 기간" : "전체 기간"}, ${term || selectedOffice !== "all" || selectedLine !== "all" ? "검색어·사업소·호선 조건을 반영해" : "모든 계량기를"} 내려받습니다${!billingOnly && selectedRisk !== "all" ? " (위험도 조건은 파일에 적용되지 않음)" : ""}`;
  // 머리글 선택 상자의 선택지: 이 보기(일일 관제·청구 확인)에 실제로 있는 호선과, 위험도별 계량기 수.
  const allViewStations = stations.filter((s) => (billingOnly ? inPeriod(s, true) : s.dataMode !== "billing_only" && inPeriod(s)));
  const lines = [...new Set(allViewStations.flatMap((s) => s.lines).filter(Boolean))].sort((a, b) => Number(a) - Number(b));
  const riskCounts = viewStations.reduce((counts, station) => ({ ...counts, [station.risk]: (counts[station.risk] || 0) + 1 }), { all: viewStations.length });
  const filters = { searchTerm, selectedLine, selectedOffice, selectedRisk };
  // 노선도 위에 보여 줄 현재 조건. 어디서 바꿨든(표 머리글·범례·검색·기간) 같은 상태를 읽는다.
  const conditions = [
    periodActive && `기간 ${periodStart} ~ ${periodEnd}`,
    term && `검색 "${searchTerm.trim()}"`,
    selectedLine !== "all" && `${selectedLine}호선`,
    selectedOffice !== "all" && (offices.find((o) => String(o.id) === selectedOffice)?.name || selectedOffice),
    !billingOnly && selectedRisk !== "all" && (riskMeta[selectedRisk]?.label || selectedRisk),
  ].filter(Boolean);
  const setters = { searchTerm: setSearchTerm, selectedLine: setSelectedLine, selectedOffice: setSelectedOffice, selectedRisk: setSelectedRisk };
  // 기간 선택·내려받기 묶음. 칸 위치가 글자 길이에 따라 움직이지 않도록 고정 격자로 둔다:
  // 1줄 [오른쪽 끝 "전체 기간" / 그 아래 기간 칸] [Excel 버튼], 2줄 안내(1줄 폭 안에서 오른쪽 정렬·줄바꿈).
  // "전체 기간"은 기본 기간이면 자리만 차지해, 나타나고 사라져도 아래 안내가 움직이지 않는다.
  const exportPanel = <div className="export-panel">
    <div className="export-panel-row">
      <div className="export-period-block">
        <button type="button" className="text-button export-reset" style={periodActive ? undefined : { visibility: "hidden" }} tabIndex={periodActive ? 0 : -1}
          aria-hidden={periodActive ? undefined : true} onClick={() => setPeriod({ start: "", end: "" })}>전체 기간</button>
        <span className="dt-date export-period">기간
          <input type="date" value={periodStart} max={periodEnd} aria-label="조회·내려받기 시작일" onChange={(e) => setPeriod((p) => ({ ...p, start: e.target.value }))} />
          ~<input type="date" value={periodEnd} min={periodStart} aria-label="조회·내려받기 종료일" onChange={(e) => setPeriod((p) => ({ ...p, end: e.target.value }))} />
        </span>
      </div>
      {exportBlocked ? <>
        {!billingOnly && <button type="button" className="secondary-button" disabled>사용량 Excel</button>}
        <button type="button" className="secondary-button" disabled>청구내역 Excel</button>
      </> : <>
        {!billingOnly && <FileLink href={exportUrl({ ...exportParams, kind: "usage" })} filename={usageFile} title={usageFile} saveAs>사용량 Excel</FileLink>}
        <FileLink href={exportUrl({ ...exportParams, kind: "bills" })} filename={billsFile} title={billsFile} saveAs>청구내역 Excel</FileLink>
      </>}
    </div>
    <p className={`export-note ${exportBlocked ? "is-error" : ""}`} role={exportBlocked ? "alert" : undefined}>{exportNote}</p>
  </div>;
  return <main className="app-shell">
    <PageHeader daily={monitored.length} alerts={monitored.filter((s) => s.risk === "alert").length} warnings={monitored.filter((s) => s.risk === "warn").length}
      dataChecks={monitored.filter((s) => s.risk === "unknown").length} />
    <CollectionPanel collection={collection} meters={data.meters || []} />
    <div className="view-tabs" role="group" aria-label="보기 선택">
      <button type="button" aria-pressed={!billingOnly} onClick={() => switchView("daily")} title="일일 사용량을 수집하는 계량기">일일 관제 <span>{dailyStations.length}</span></button>
      <button type="button" aria-pressed={billingOnly} onClick={() => switchView("billing")} title="일일 관제를 포함한 모든 계량기의 최근 청구서">청구 확인 <span>{billingStations.length}</span></button>
    </div>
    <section className="workspace">
      <MapPanel stationMap={new Map(filteredStations.map((s) => [s.id, s]))} locations={data.locations || {}} billingOnly={billingOnly}
        selectedRisk={selectedRisk} onSelectRisk={setSelectedRisk} riskCounts={riskCounts} conditions={conditions} onReset={resetFilters}>
        {!billingOnly && <section className="side-block" aria-label="우선 확인할 계량기">
          <SectionTitle as="h2">우선 확인 대상 <span className="count">{alerts.length + warnings.length}개</span></SectionTitle>
          <div className="priority-list">
            {[...alerts, ...warnings].map((station) => <StationRow station={station} key={station.id} />)}
            {!alerts.length && !warnings.length && <EmptyNote>경고·주의 계량기가 없습니다.</EmptyNote>}
          </div>
          {selectedRisk === "unknown"
            ? <button type="button" className="text-button" onClick={() => setSelectedRisk("all")}>자료 확인 계량기 {unknowns.length}개만 보는 중 · 전체 위험도 보기</button>
            : unknowns.length > 0 && <button type="button" className="text-button" onClick={() => setSelectedRisk("unknown")}>자료 확인이 필요한 계량기 {unknowns.length}개 보기</button>}
        </section>}
      </MapPanel>
    </section>
    <StationTable stations={sortedStations} total={allViewStations.length} filtering={filtering} onReset={resetFilters}
      filters={filters} onFilterChange={(key, value) => setters[key](value)} options={{ offices, lines, riskCounts }}
      sortKey={sortKey} onSortChange={setSortKey} billingOnly={billingOnly} actions={exportPanel} />
  </main>;
}
