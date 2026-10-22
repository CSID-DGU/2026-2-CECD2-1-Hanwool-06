import { useState, useEffect, useMemo, useRef } from "react";
import { buildStations, CONTRACT_NOTE, CONTRACT_TAG, latestDate, kdate, ton, won, detailHref, isDetailDate, signedPct, CHART_COLORS, SEVERITY_TONE } from "./data.js";
import { Card, EmptyNote, Stat } from "./ui";
import LineChart from "./LineChart";
import RiskCalendar from "./RiskCalendar";
import Bill from "./Bill";
import { exportUrl } from "../../api.js";
import FileLink from "../../components/FileLink.jsx";
import CollectionPanel from "../../components/CollectionPanel.jsx";
import { SelectField } from "../../components/Dropdown.jsx";
import RiskBadge from "../Dashboard/components/RiskBadge.jsx";
import { lineColors } from "../Dashboard/dashboardData.js";

export default function DetailPage({ data, hash, collection }) {
  const stations = useMemo(() => buildStations(data.bills, data.stations, data.daily, data.risk, data.meters), [data]);
  const latest = useMemo(() => latestDate(stations), [stations]);
  const params = new URLSearchParams(hash.split("?")[1] || "");
  const meterId = params.get("meter");
  const requestedDate = isDetailDate(params.get("date")) ? params.get("date") : "";
  const requestedStation = stations.find((s) => s.lines.some((l) => l.meterId === meterId));
  const station = requestedStation || (meterId ? null : stations.find((s) => s.lines.some((l) => l.risk)) || stations[0]);
  if (!station) return <main className="management-page"><h1>{meterId ? "계량기를 찾을 수 없습니다" : "등록된 계량기가 없습니다"}</h1>
    <p>{meterId ? "비활성 계량기이거나 조회 권한이 없는 계량기입니다." : "계량기 관리에서 먼저 등록하세요."}</p><a className="secondary-button" href="#/meters">계량기 관리로 이동</a></main>;
  const line = station.lines.find((l) => l.meterId === meterId) || station.lines.find((l) => l.risk) || station.lines[0];
  const officeId = String(line.office_id || "");
  const officeStations = stations.filter((s) => s.lines.some((l) => String(l.office_id || "") === officeId));
  const selectMeter = (record) => { window.location.hash = detailHref(record.meterId).slice(1); };
  const sameLine = station.lines.filter((l) => String(l.line) === String(line.line));
  const viewStation = { ...station, 주소: line.주소, 용도: line.용도, 사용자명: station.역명 };
  const stale = Boolean(line.risk && data.status?.reference_date && line.risk.기준일 < data.status.reference_date);
  const billingOnly = line.dataMode === "billing_only";
  const dateRisk = requestedDate ? line.riskDays.find((day) => day.date === requestedDate) : null;
  const riskNote = requestedDate ? dateRisk?.note : line.riskNote;
  const fileBase = [station.역명, line.line && `${line.line}호선`, line.고객번호].filter(Boolean).join("_");   // 서버가 붙이는 이름과 같다
  return <main className="dt-page">
    <div className="dt-picker" aria-label="조회할 계량기 선택">
      <SelectField label="영업사업소" value={officeId} options={(data.offices || []).map((office) => ({ value: String(office.id), label: office.name }))} onChange={(value) => {
        const record = stations.flatMap((s) => s.lines).find((l) => String(l.office_id) === value);
        if (record) selectMeter(record);
      }} />
      <SelectField label="역" className="dt-picker-station" value={String(station.id)} options={officeStations.map((s) => ({ value: String(s.id), label: s.역명 }))} searchable
        searchPlaceholder="역명 검색" emptyText="일치하는 역이 없습니다." onChange={(value) => {
          const next = stations.find((s) => String(s.id) === value);
          if (next) selectMeter(next.lines.find((l) => String(l.office_id || "") === officeId) || next.lines[0]);
        }} />
      <SelectField label="계량기" className="dt-picker-meter" value={line.meterId} options={sameLine.map((l) => ({ value: l.meterId, label: `${l.display_name || "기본 계량기"} (${l.고객번호})${l.dataMode !== "billing_only" ? " · 일일 관제" : CONTRACT_TAG[l.contractKind] ? ` · ${CONTRACT_TAG[l.contractKind]}` : ""}` }))}
        onChange={(value) => selectMeter(station.lines.find((l) => l.meterId === value))} />
    </div>
    <StationHeader station={station} line={line} selectMeter={selectMeter} requestedDate={requestedDate} dateRisk={dateRisk} />
    <CollectionPanel collection={collection} meters={data.meters || []} meterId={line.meterId} />
    {requestedDate && <p className="data-notice" role="status">조회 기준일: {requestedDate}{billingOnly ? " · 청구서만 수집하는 계량기라 일일 사용량과 위험도 분석이 없습니다." : " · 해당 날짜의 일일 사용량과 위험도를 표시합니다."}</p>}
    {params.has("date") && !requestedDate && <p className="data-notice" role="status">조회 날짜가 올바르지 않아 최신 자료를 표시합니다. 날짜는 YYYY-MM-DD 형식으로 입력해 주세요.</p>}
    {riskNote?.reason ? <StallNotice note={riskNote} meterNumber={line.계량기번호} />
      : !requestedDate && (stale || line.riskError) && <p className="data-notice" role="status">{line.riskError ? "최근 관측값에 오류가 있어 위험도를 새로 계산하지 못했습니다." : "이 계량기의 위험도는 최신 분석일보다 오래된 자료입니다."}{line.risk ? ` 아래 위험도는 ${line.risk.기준일} 기준입니다.` : ""}</p>}
    {!billingOnly && <div className="dt-cols">
      {line.dataMode === "daily"
        ? <DailyPanel key={`daily-${line.meterId}-${requestedDate}`} usage={line.daily} ridership={line.ridership} latest={latest} requestedDate={requestedDate} />
        : <Card title="일일 사용량"><EmptyNote>아직 수집된 일일 사용량이 없습니다. 첫 자료가 들어오면 여기에 표시됩니다.</EmptyNote></Card>}
      {/* 같은 줄의 두 카드는 key가 서로 달라야 한다. 같으면 계량기를 바꿀 때 지난 카드가 남아 쌓인다. */}
      {(line.dataMode === "daily" || requestedDate) && (line.riskDays.length || requestedDate ? <RiskPanel key={`risk-${line.meterId}-${requestedDate}`} line={line} requestedDate={requestedDate} /> : <Card title="위험도"><EmptyNote>{line.historyShort ? "분석 대기(학습 데이터 부족): 모델이 배울 과거 자료가 90일에 못 미쳐 아직 판정하지 않습니다." : "아직 위험도 분석 결과가 없습니다."}</EmptyNote></Card>)}
    </div>}
    <Card title="청구서">
      <Bill key={line.meterId} station={viewStation} line={line} />
    </Card>
    <div className="export-toolbar"><span>이 계량기 자료 내려받기</span>
      {!billingOnly && <FileLink href={exportUrl({ kind: "usage", meter_id: line.meterId })} filename={`${fileBase}_사용량.xlsx`}>일일 사용량 Excel</FileLink>}
      <FileLink href={exportUrl({ kind: "bills", meter_id: line.meterId })} filename={`${fileBase}_청구내역.xlsx`}>청구내역 Excel</FileLink></div>
  </main>;
}

// 원격검침 값이 그대로일 때: 확실한 사실만 쓰고 원인은 단정하지 않는다. 현장에서 가르는 방법을 함께 둔다.
function StallNotice({ note, meterNumber }) {
  const stalled = note.reason === "값 그대로";
  return <div className="data-notice" role="status">
    <p>{stalled
      ? `원격검침 값이 ${kdate(note.since)}부터 ${note.days}일째 그대로라(매일 0톤) 위험도를 판정하지 않습니다. 원격 단말이나 계량기 이상인지, 실제로 물을 쓰지 않은 것인지는 이 자료로 알 수 없습니다.`
      : `원격검침 값이 ${note.days}일 동안 그대로였다가 다시 움직인 첫날이라 이날은 판정하지 않습니다. 다음 날부터는 다시 판정합니다.`}</p>
    {stalled && <details className="data-notice-more"><summary>확인 방법</summary><ol>
      <li>현장에서 계량기{meterNumber ? `(${meterNumber})` : ""} 숫자를 읽어 아리수 원격검침 조회의 지침값과 비교합니다. 계량기 숫자가 더 크면 원격 단말 쪽, 같으면 계량기나 급수 쪽입니다.</li>
      <li>숫자가 같으면 물을 틀었을 때 계량기가 도는지 봅니다.</li>
      <li>관할 수도사업소나 계측관리과(02-3146-1255, 1257)에 고객번호와 계량기번호로 원격검침 상태를 문의합니다.</li>
    </ol></details>}
  </div>;
}

function StationHeader({ station, line, selectMeter, requestedDate, dateRisk }) {
  const lines = [...new Set(station.lines.map((l) => l.line))].filter(Boolean);
  const risk = requestedDate ? dateRisk : line.risk;
  const note = requestedDate ? dateRisk?.note : line.riskNote;
  return <section className="dt-station">
    <div>
      <div className="dt-station-lines">{lines.map((number) => <button key={number} type="button" className="line-badge" style={{ "--line-color": lineColors[number] || "#667085" }} onClick={() => selectMeter(station.lines.find((l) => l.line === number))} aria-pressed={number === line.line} aria-label={`${number}호선`}>{number}</button>)}</div>
      <h1>{station.역명}</h1>
      <p className="dt-meter-label">{line.display_name || "기본 계량기"} <span>고객번호 {line.고객번호}</span></p>
      <dl className="dt-facts">
        <div><dt>영업사업소</dt><dd>{line.영업사업소}</dd></div>
        <div><dt>위치</dt><dd>{line.주소}</dd></div>
        <div><dt>수집 자료</dt><dd>{line.dataMode === "billing_only" ? "청구서" : line.dataMode === "pending" ? "일일 사용량(첫 수집 대기), 청구서" : "일일 사용량, 청구서"}</dd></div>
        {CONTRACT_TAG[line.contractKind] && <div><dt>계약 종류</dt><dd>{CONTRACT_TAG[line.contractKind]}: {CONTRACT_NOTE[line.contractKind]}</dd></div>}
      </dl>
    </div>
    {note?.reason ? <div className="dt-station-risk"><RiskBadge risk="unknown" /><strong>{note.reason === "값 그대로" ? `${note.days}일째` : "첫날"}</strong><span>{note.reason === "값 그대로" ? "원격검침 값 그대로" : "원격검침 값 다시 움직임"}</span></div>
      : risk && <div className="dt-station-risk"><RiskBadge risk={SEVERITY_TONE[risk.severity] || "unknown"} /><strong>{signedPct(risk.pct)}</strong><span>예측 대비</span></div>}
  </section>;
}

// 위험도: 달력에서 고른 날(처음에는 마지막 판정일)의 수치를 보여 준다.
function RiskPanel({ line, requestedDate }) {
  const days = line.riskDays;
  const [selected, setSelected] = useState(requestedDate || days.at(-1)?.date || "");
  const day = days.find((d) => d.date === selected);
  const note = day?.note;
  const msg = note?.reason === "값 그대로"
    ? `원격검침 값이 그대로라(0톤) 판정하지 않았습니다. ${kdate(note.since)}부터 ${note.days}일째, 멈추기 전 평소 하루 ${ton(note.usual)}톤.`
    : note?.reason === "다시 움직인 첫날"
      ? `그대로였던 원격검침 값이 다시 움직인 첫날이라 판정하지 않았습니다. 이날 ${ton(note.actual)}톤, 멈추기 전 평소 하루 ${ton(note.usual)}톤.${note.actual >= note.usual * 3 ? " 그동안의 양이 한꺼번에 들어왔을 수 있습니다." : ""}`
    : day?.err
    ? "이 날 관측값에 오류가 있어 판정하지 못했습니다."
    : day?.severity === "정상"
      ? "사용량이 예측 범위 안에 있습니다."
      : day ? `예측보다 ${ton(Math.abs(day.error))}톤(${signedPct(day.pct, "비율 없음")}) ${day.방향 === "과다" ? "많이" : "적게"} 썼습니다.` : "";
  return (
    <Card title="위험도" right={`${kdate(selected)} 기준`}>
      {day ? <>
      <p className="dt-risk-head"><RiskBadge risk={SEVERITY_TONE[day.severity] || "unknown"} /> {msg}</p>
      <dl className="dt-risk-stats">
        <Stat label="예측" value={Number.isFinite(day.predicted) ? `${ton(day.predicted)} 톤` : "—"} />
        <Stat label="실제" value={`${ton(day.actual)} 톤`} />
        <Stat label="오차" value={Number.isFinite(day.error) ? `${day.error > 0 ? "+" : ""}${ton(day.error)} 톤` : "—"} />
        <Stat label="예측 대비" value={signedPct(day.pct, "비율 없음")} />
      </dl>
      </> : <EmptyNote>{selected}에는 위험도 분석 결과가 없습니다.</EmptyNote>}
      <RiskCalendar days={days} selected={selected} onSelect={setSelected} />
      <p className="form-hint">달력에서 날짜를 누르면 그날 수치를 봅니다. 위험도는 예측 오차를 이 계량기의 최근 사용량과 평소 변동 폭에 견줘 정합니다.</p>
    </Card>
  );
}

// 일일 사용량과 승하차를 한 날짜로 함께 본다.
function DailyPanel({ usage, ridership, latest, requestedDate }) {
  const series = [usage, ridership].filter((points) => points?.length);
  const maxDate = series.length
    ? series
        .map((s) => s[s.length - 1].date)
        .sort()
        .slice(-1)[0]
    : latest;
  const [date, setDate] = useState(requestedDate || maxDate);
  const previousMax = useRef(maxDate);
  useEffect(() => {
    if (!requestedDate) setDate((current) => !current || current === previousMax.current ? maxDate : current);
    previousMax.current = maxDate;
  }, [maxDate, requestedDate]);

  if (series.length === 0) {
    return (
      <Card title="일일 사용량·승하차">
        <EmptyNote>수집된 일일 자료가 없습니다.</EmptyNote>
      </Card>
    );
  }

  const minDate = [requestedDate, ...series.map((s) => s[0].date)].filter(Boolean).sort()[0];
  const inputMaxDate = requestedDate > maxDate ? requestedDate : maxDate;

  return (
    <Card title="일일 사용량·승하차" right={
      <label className="dt-date">
        날짜
        <input type="date" value={date} min={minDate} max={inputMaxDate} onChange={(e) => setDate(e.target.value)} />
      </label>
    }>
      <DailySub label="사용량" unit="톤" color={CHART_COLORS.usage} series={usage} date={date} fmt={ton} />
      <DailySub label="승하차" unit="명" color={CHART_COLORS.riders} series={ridership} date={date} fmt={won} />
    </Card>
  );
}

function DailySub({ label, unit, color, series, date, fmt }) {
  if (!series?.length) {
    return (
      <div className="dt-daily-sub">
        <p className="dt-daily-sub-head"><span>{label}</span> 자료 없음</p>
      </div>
    );
  }
  const maxDate = series[series.length - 1].date;
  const point = series.find((p) => p.date === date);
  const cutoff = date <= maxDate ? date : maxDate;
  const before = new Date(`${cutoff}T00:00:00Z`);
  before.setUTCDate(before.getUTCDate() - 6);
  const start = Number.isNaN(before.getTime()) ? "" : before.toISOString().slice(0, 10);
  const recent = series.filter((p) => p.date >= start && p.date <= cutoff);

  return (
    <div className="dt-daily-sub">
      <p className="dt-daily-sub-head">
        <span>{label}</span>
        {point ? <><strong>{fmt(point.value)}</strong> {unit}</> : `이 날짜 자료 없음 (마지막 수집일 ${maxDate})`}
      </p>
      <LineChart
        series={[{ color, values: recent.map((p) => p.value), fill: true }]}
        xLabels={recent.map((p) => p.date.slice(5))}
        height={200}
        labelEvery={1}
        showValues
        formatValue={fmt}
        formatY={fmt}
        yUnit={unit}
        xUnit="월/일"
      />
    </div>
  );
}
