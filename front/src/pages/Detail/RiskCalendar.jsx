import { useEffect, useState } from "react";
import { monthCells, monthOf, shiftMonth, SEVERITY_TONE } from "./data.js";

const DOW = ["일", "월", "화", "수", "목", "금", "토"];
const monthLabel = (ym) => `${ym.slice(0, 4)}년 ${Number(ym.slice(5))}월`;

// 날짜별 위험도 달력. 정상은 색이 없고 주의·경고만 색을 띤다. 날짜를 누르면 그날 수치를 본다.
export default function RiskCalendar({ days, selected, onSelect }) {
  const months = [selected, days[0]?.date, days.at(-1)?.date].filter(Boolean).map(monthOf).sort();
  const first = months[0], last = months.at(-1);
  const [ym, setYm] = useState(monthOf(selected || days.at(-1)?.date));
  useEffect(() => { if (selected) setYm(monthOf(selected)); }, [selected]);
  const cells = monthCells(ym, days);
  const tone = (cell) => cell.severity ? SEVERITY_TONE[cell.severity] || "unknown" : "empty";
  return (
    <div className="dt-cal">
      <div className="dt-cal-head">
        <button type="button" className="secondary-button" onClick={() => setYm(shiftMonth(ym, -1))} disabled={ym <= first}>이전 달</button>
        <strong>{monthLabel(ym)}</strong>
        <button type="button" className="secondary-button" onClick={() => setYm(shiftMonth(ym, 1))} disabled={ym >= last}>다음 달</button>
      </div>
      <div className="dt-cal-grid" aria-label={`${monthLabel(ym)} 날짜별 위험도`}>
        {DOW.map((d) => <span key={d} className="dt-cal-dow">{d}</span>)}
        {cells.map((cell, i) => cell
          ? <button key={cell.date} type="button" className={`dt-cal-day is-${tone(cell)}`} disabled={!cell.severity}
              aria-pressed={cell.date === selected} aria-label={`${Number(ym.slice(5))}월 ${cell.day}일 ${cell.severity || "판정 없음"}`}
              onClick={() => onSelect(cell.date)}>{cell.day}</button>
          : <span key={`blank-${i}`} aria-hidden="true" />)}
      </div>
      <p className="dt-cal-legend">
        <span className="is-warn">주의</span>
        <span className="is-alert">경고</span>
        <span className="is-unknown">자료 확인</span>
        <span className="is-empty">판정 없음</span>
      </p>
    </div>
  );
}
