import DailyTag from "./DailyTag.jsx";
import LineBadges from "./LineBadges.jsx";
import RiskBadge from "./RiskBadge.jsx";
import { EmptyNote, SectionTitle } from "../../Detail/ui.jsx";
import { detailHref, signedPct, ton, won } from "../../Detail/data.js";
import { nextSort, riskMeta } from "../dashboardData.js";
import { FilterHeader, SortHeader } from "../../../components/TableHeader.jsx";

const DATE_HINTS = { asc: "오래된순, 누르면 최신순", desc: "최신순, 누르면 오래된순", idle: "누르면 최신순" };
const NUMBER_HINTS = { asc: "작은 값부터, 누르면 큰 값부터", desc: "큰 값부터, 누르면 작은 값부터", idle: "누르면 큰 값부터" };

// 머리글에서 바로 정렬(일일 관제는 역명·일 사용량·예측 대비·기준일, 청구 확인은 역명·최근 청구월·납부금액)하고 고른다(호선·영업사업소, 일일 관제는 위험도도). 검색과 조건 초기화는 표 바로 위에 둔다.
// stations: 조건을 적용하고 정렬까지 끝낸 목록. total: 조건을 적용하기 전 이 보기(일일 관제·청구 확인)의 계량기 수.
export default function StationTable({ stations, total = stations.length, filtering = false, onReset, filters = {}, onFilterChange, options = {},
  sortKey = "name-asc", onSortChange, billingOnly = false, actions }) {
  const { offices = [], lines = [], riskCounts = {} } = options;
  const sort = (column) => onSortChange?.(nextSort(sortKey, column));
  const filter = (key) => (next) => onFilterChange?.(key, next);
  const riskItems = Object.entries(riskMeta).filter(([risk]) => !["all", "billing_only"].includes(risk))
    .map(([risk, meta]) => ({ value: risk, label: `${meta.label} ${riskCounts[risk] || 0}개` }));
  return (
    <section className="table-panel">
      <SectionTitle as="h2" right={actions}>
        {billingOnly ? "계량기별 최근 청구" : "계량기 목록"}{" "}
        <span className="count" aria-live="polite">{filtering ? `${stations.length}개 (전체 ${total}개 중)` : `${stations.length}개`}</span>
      </SectionTitle>
      <div className="table-toolbar">
        <label className="form-field table-search">검색
          <input type="search" value={filters.searchTerm ?? ""} onChange={(event) => onFilterChange?.("searchTerm", event.target.value)} placeholder="역명, 계량기 이름, 고객번호, 사업소" autoComplete="off" />
        </label>
        <p className="form-hint">{billingOnly
          ? "머리글에서 역명·최근 청구월·납부금액은 정렬, 호선·영업사업소는 조건을 고를 수 있습니다. 노선도도 같은 조건을 따릅니다."
          : "머리글에서 역명·사용량·예측 대비·기준일은 정렬, 호선·영업사업소·위험도는 조건을 고를 수 있습니다. 노선도와 우선 확인 대상도 같은 조건을 따릅니다."}</p>
        {filtering && <button type="button" className="text-button" onClick={onReset}>조건 초기화</button>}
      </div>
      <div className="table-wrap table-wrap--fixed">
        <table className={`data-table data-table--controls ${billingOnly ? "" : "data-table--wide"}`}>
          {/* 조건·숫자 열은 가장 긴 글("종합운동장영업사업소", "첫 수집 대기 00개")이 잘리지 않는 고정 폭,
              역명 열이 남은 폭을 가진다. 검색·조건을 바꿔도 열이 흔들리지 않는다. */}
          <colgroup>
            {(billingOnly ? [null, 120, 170, 150, 150] : [null, 120, 170, 200, 110, 110, 150])
              .map((width, i) => <col key={i} style={width ? { width: `${width}px` } : undefined} />)}
          </colgroup>
          <thead>
            <tr>
              <SortHeader column="name" label="역명" sortKey={sortKey} onSort={sort} />
              <FilterHeader label="호선" value={filters.selectedLine} onChange={filter("selectedLine")} items={lines.map((line) => ({ value: line, label: `${line}호선` }))} />
              <FilterHeader label="영업사업소" value={filters.selectedOffice} onChange={filter("selectedOffice")} items={offices.map((office) => ({ value: String(office.id), label: office.name }))} />
              {!billingOnly && <>
                <FilterHeader label="위험도" value={filters.selectedRisk} onChange={filter("selectedRisk")} items={riskItems} />
                <SortHeader column="usage" label="일 사용량" sortKey={sortKey} onSort={sort} className="num" hints={NUMBER_HINTS} />
                <SortHeader column="delta" label="예측 대비" sortKey={sortKey} onSort={sort} className="num" hints={NUMBER_HINTS} />
              </>}
              <SortHeader column="date" label={billingOnly ? "최근 청구월" : "기준일"} sortKey={sortKey} onSort={sort} hints={DATE_HINTS} />
              {billingOnly && <SortHeader column="amount" label="납부금액" sortKey={sortKey} onSort={sort} className="num" hints={NUMBER_HINTS} />}
            </tr>
          </thead>
          <tbody>
            {stations.map((station) => (
              <tr key={station.id}>
                <td>
                  <a className="station-link" href={detailHref(station.id)}>{station.name}</a>
                  <small>{station.displayName} {station.customerNo}
                    {billingOnly && station.dataMode !== "billing_only" ? <DailyTag /> : station.contractTag && <span className="contract-tag">{station.contractTag}</span>}</small>
                </td>
                <td><LineBadges lines={station.lines} /></td>
                <td>{station.office}</td>
                {!billingOnly && <>
                  <td>
                    <RiskBadge risk={station.risk} label={station.riskLabel} />
                    {station.riskDetail && <small>{station.riskDetail}</small>}
                  </td>
                  <td className="num">{ton(station.usage)} 톤</td>
                  <td className="num">{signedPct(station.delta)}</td>
                </>}
                <td>{billingOnly ? station.latestBillMonth || "청구서 없음" : <>사용량 {station.dailyDate || "없음"}<small>분석 {station.riskDate || "대기"}</small></>}</td>
                {billingOnly && <td className="num">{station.latestBillAmount == null ? "—" : `${won(station.latestBillAmount)} 원`}</td>}
              </tr>
            ))}
          </tbody>
        </table>
        {stations.length === 0 ? <EmptyNote>
          {filtering ? "검색어·조건에 맞는 계량기가 없습니다." : "표시할 계량기가 없습니다."}
          {filtering && onReset && <> <button type="button" className="text-button" onClick={onReset}>조건 초기화</button></>}
        </EmptyNote> : null}
      </div>
    </section>
  );
}
