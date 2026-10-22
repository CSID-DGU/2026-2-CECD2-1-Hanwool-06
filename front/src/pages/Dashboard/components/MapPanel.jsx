import { useRef, useState } from "react";

const LABEL_LIMIT = 12;   // 이 개수 이하로 걸러지면 노선도에 역명을 함께 그린다
import { buildMapGroups, labelSide, meterName, riskMeta } from "../dashboardData.js";
import { detailHref, ton, won } from "../../Detail/data.js";
import { EmptyNote, SectionTitle } from "../../Detail/ui.jsx";
import DailyTag from "./DailyTag.jsx";
import RiskBadge from "./RiskBadge.jsx";
import StationRow from "./StationRow.jsx";

// 노선도에서 고른 역의 계량기. 일일 관제는 위험도를, 청구 확인은 최근 청구 내역을 보여 준다.
function StationSelection({ station, billingOnly, onClose }) {
  return <section className="side-block" aria-label="선택한 역의 계량기" aria-live="polite">
    <SectionTitle as="h2" right={station && <button type="button" className="text-button" onClick={onClose}>닫기</button>}>
      {station ? <>{station.name} <span className="count">계량기 {station.meters.length}개</span></> : "선택한 역"}
    </SectionTitle>
    <div className="priority-list">
      {!station ? <EmptyNote>노선도에서 역을 누르면 여기에 나옵니다.</EmptyNote>
        : !billingOnly ? station.meters.map((meter) => <StationRow key={meter.id} station={meter} byMeter />)
        : station.meters.map((meter) => <a key={meter.id} href={detailHref(meter.id)} className="station-card">
          <div className="station-card-head">
            <strong>{meter.lines.filter(Boolean).map((line) => `${line}호선`).join(", ")} {meterName(meter)}</strong>
            {/* 일일 사용량까지 보는 계량기(첫 수집 대기 포함)는 오른쪽 위에 표시하고, 수돗물 계약이 아니면 그 종류를 적는다 */}
            {meter.dataMode !== "billing_only" ? <DailyTag /> : meter.contractTag && <span className="contract-tag">{meter.contractTag}</span>}
          </div>
          <small>고객번호 {meter.customerNo}</small>
          <dl>
            <div><dt>영업사업소</dt><dd>{meter.office}</dd></div>
            <div><dt>최근 청구</dt><dd>{meter.latestBillMonth || "청구서 없음"}</dd></div>
            {meter.latestBillMonth && <>
              <div><dt>사용량</dt><dd>{meter.latestBillUsage == null ? "—" : `${ton(meter.latestBillUsage)} 톤`}</dd></div>
              <div><dt>납부금액</dt><dd>{meter.latestBillAmount == null ? "—" : `${won(meter.latestBillAmount)} 원`}</dd></div>
            </>}
          </dl>
        </a>)}
    </div>
  </section>;
}

// children: 오른쪽 칸에서 고른 역 위에 놓을 내용(일일 관제의 우선 확인 대상).
// selectedRisk·onSelectRisk·riskCounts: 범례를 위험도 선택 버튼으로 쓴다(표 머리글의 위험도 조건과 같은 상태).
// conditions·onReset: 조건이 걸려 있을 때 노선도 위에 보여 주는 현재 조건 요약과 초기화.
export default function MapPanel({ stationMap, locations, billingOnly = false, children, selectedRisk = "all", onSelectRisk, riskCounts = {}, conditions = [], onReset }) {
  const [zoom, setZoom] = useState(1);
  const [selected, setSelected] = useState(null);
  const [dragging, setDragging] = useState(false);
  const viewport = useRef(null);
  const drag = useRef(null);
  const groups = buildMapGroups([...stationMap.values()], locations, billingOnly);
  const mapped = groups.filter((s) => s.position);
  // 조건으로 걸러져 노드가 적을 때는 역명을 함께 그린다. 12개까지는 노선도 폭에서 라벨이 서로 거의 겹치지 않는다.
  const labelled = mapped.length > 0 && mapped.length <= LABEL_LIMIT;
  const points = mapped.map((station) => ({ id: station.id, x: station.position.x * 25.51, y: station.position.y * 21.23 }));
  const unplaced = groups.filter((s) => !s.position);
  const active = groups.find((s) => s.id === selected);
  // 끌어서 옮기기는 확대해서 스크롤할 내용이 있을 때만 뜻이 있다. 100%에서는 끌 수 없으므로 손 모양 커서도 보이지 않는다.
  const draggable = zoom > 1;
  const fit = () => { setZoom(1); viewport.current?.scrollTo(0, 0); };
  const startDrag = (e) => {
    if (!draggable || e.pointerType !== "mouse" || e.button !== 0 || e.target.closest("[data-station]")) return;
    drag.current = { x: e.clientX, y: e.clientY, left: viewport.current.scrollLeft, top: viewport.current.scrollTop };
    e.currentTarget.setPointerCapture(e.pointerId);
    setDragging(true);
  };
  const moveDrag = (e) => {
    if (!drag.current) return;
    viewport.current.scrollLeft = drag.current.left - e.clientX + drag.current.x;
    viewport.current.scrollTop = drag.current.top - e.clientY + drag.current.y;
  };
  const endDrag = () => { drag.current = null; setDragging(false); };
  return <><section className="map-panel" aria-label="지하철 노선도">
    <SectionTitle as="h2" right={<div className="zoom-controls">
      <button type="button" onClick={() => setZoom((v) => Math.max(1, v - .5))} disabled={zoom <= 1} aria-label="노선도 축소">−</button>
      <span>{Math.round(zoom * 100)}%</span>
      <button type="button" onClick={() => setZoom((v) => Math.min(4, v + .5))} disabled={zoom >= 4} aria-label="노선도 확대">+</button>
      <button type="button" onClick={fit}>전체 보기</button>
    </div>}>노선도</SectionTitle>
    {conditions.length > 0 && <p className="map-conditions" aria-live="polite">현재 조건: {conditions.map((item, i) => <strong key={i}>{item}</strong>)}
      {onReset && <button type="button" className="text-button" onClick={onReset}>조건 초기화</button>}</p>}
    <div ref={viewport} className={`metro-viewport ${draggable ? "is-draggable" : ""} ${dragging ? "is-dragging" : ""}`} tabIndex={0}
      aria-label={draggable ? "노선도. 스크롤하거나 끌어서 옮길 수 있습니다." : "노선도. 확대하면 스크롤하거나 끌어서 옮길 수 있습니다."}
      onPointerDown={startDrag} onPointerMove={moveDrag} onPointerUp={endDrag} onPointerCancel={endDrag}>
      <svg className="metro-canvas" viewBox="0 0 2551 2123" style={{ width: `${zoom * 100}%`, height: `${zoom * 100}%` }} role="group" aria-label="서울 지하철 노선도와 계량기가 있는 역">
        <image href="/metro_map_rectangle.png" width="2551" height="2123" aria-hidden="true" />
        {mapped.map((station) => {
          const x = station.position.x * 25.51, y = station.position.y * 21.23;
          const side = labelled ? labelSide({ id: station.id, x, y }, points) : null;
          const label = `${station.name}, 계량기 ${station.meters.length}개, ${billingOnly ? (station.daily ? "일일 관제 포함" : "청구 전용") : riskMeta[station.risk].label}`;
          return <g key={station.id} transform={`translate(${x} ${y})`} className={`metro-node metro-node--${station.risk}${billingOnly && station.daily ? " metro-node--daily" : ""}`} data-station={station.id} role="button" tabIndex={0} aria-label={label} aria-pressed={selected === station.id} onClick={() => setSelected(station.id)} onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setSelected(station.id); } }}>
            <title>{label}</title><circle className="metro-node-hit" r="33" /><circle className="metro-node-dot" r="18" />
            {labelled && <text className="metro-node-name" x={side === "above" ? 0 : 28} y={side === "above" ? -26 : 9} textAnchor={side === "above" ? "middle" : "start"}>{station.name}</text>}
            {/* 청구 확인 보기는 계량기가 2개 이상인 역에 그 수를 적는다. 1까지 적으면 노선도의 ①호선 배지처럼 보인다 */}
            <text y="7" textAnchor="middle" className="metro-node-symbol">{billingOnly ? (station.meters.length > 1 ? station.meters.length : "") : station.risk === "alert" ? "!" : ["unknown", "analysis_pending", "pending"].includes(station.risk) ? "" : station.meters.length > 1 ? station.meters.length : ""}</text>
          </g>;
        })}
      </svg>
    </div>
    <div className="map-legend" role={billingOnly ? undefined : "group"} aria-label={billingOnly ? undefined : "위험도 선택"}>
      {billingOnly ? <><span className="risk-badge risk-daily">일일 관제 포함</span><RiskBadge risk="billing_only" /></>
        : ["alert", "warn", "ok", "unknown", "analysis_pending", "pending"].map((risk) => (
          <button key={risk} type="button" className="map-legend-button" aria-pressed={selectedRisk === risk} title={selectedRisk === risk ? "누르면 전체 보기" : `${riskMeta[risk].label}만 보기`}
            onClick={() => onSelectRisk?.(selectedRisk === risk ? "all" : risk)}>
            <RiskBadge risk={risk} /><span className="count">{riskCounts[risk] || 0}</span>
          </button>))}
    </div>
    <p className="map-legend-help">{billingOnly ? "숫자는 그 역의 계량기 수입니다(숫자가 없으면 1개). 역을 누르면 그 역의 계량기와 최근 청구 내역을 볼 수 있습니다." : "범례를 누르면 그 위험도만 보입니다. 역을 누르면 그 역의 계량기를 볼 수 있습니다."}</p>
    {unplaced.length > 0 && <details className="map-unplaced"><summary>노선도에 위치가 없는 역 {unplaced.length}개</summary><ul>{unplaced.map((station) => <li key={station.id}><button type="button" className="text-button" onClick={() => setSelected(station.id)}>{station.name} ({station.meters.length}개)</button></li>)}</ul></details>}
    <p className="map-source">노선도 출처: 서울교통공사</p>
  </section>
  <aside className="side-panel">
    {children}
    <StationSelection station={active} billingOnly={billingOnly} onClose={() => setSelected(null)} />
  </aside>
  </>;
}
