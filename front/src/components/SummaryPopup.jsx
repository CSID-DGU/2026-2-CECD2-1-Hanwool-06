import { useEffect, useRef, useState } from "react";
import { analyzeCause, getSummary, sendAlert } from "../api.js";
import { detailHref, ton, signedPct, SEVERITY_TONE } from "../pages/Detail/data.js";
import RiskBadge from "../pages/Dashboard/components/RiskBadge.jsx";
import "./agent.css";

const withLine = (name = "") => name.replace(/^(.+역)(\d+)$/, "$1 $2호선");
function extractUrl(text) {
  const match = /https?:\/\/[^\s<>]+/.exec(text || "");
  return match ? match[0] : null;
}

const taskKey = (meterId, day) => `${meterId}|${day}`;

// 창을 닫아도 원인 분석과 메일 발송이 이어지도록 상태는 창 밖에 둔다. 로그아웃하면 AgentChat처럼 함께 사라진다.
export default function SummaryPopup({ open, date, onClose }) {
  const [causes, setCauses] = useState({});
  const [mails, setMails] = useState({});
  async function analyze(meterId, day) {
    const key = taskKey(meterId, day);
    const save = (cause) => setCauses((current) => ({ ...current, [key]: cause }));
    save({ loading: true });
    try { const result = await analyzeCause(meterId, day); save({ data: result.analysis || result }); }
    catch (e) { save({ error: e.message }); }
  }
  async function sendMail(meterId, day) {
    const key = taskKey(meterId, day);
    const sent = Boolean(mails[key]?.sent);   // 한 번이라도 보냈으면 다음 발송 때 다시 묻는다.
    const save = (next) => setMails((current) => ({ ...current, [key]: next }));
    save({ loading: true, sent });
    try {
      const result = await sendAlert(meterId, day);
      save({ sent: sent || Boolean(result.sent || result.to?.length), ok: Boolean(result.sent), message: result.sent ? `메일을 보냈습니다${result.to?.length ? ` (${result.to.join(", ")})` : ""}.` : result.reason || "메일을 보내지 못했습니다." });
    } catch (e) { save({ sent, ok: false, message: e.message }); }
  }
  return open ? <SummaryDialog date={date} tasks={{ causes, mails, analyze, sendMail }} onClose={onClose} /> : null;
}

function SummaryDialog({ date, tasks, onClose }) {
  const dialog = useRef(null);
  const pressedOutside = useRef(false);
  const [state, setState] = useState({ loading: true });
  const [revision, setRevision] = useState(0);
  useEffect(() => { dialog.current.showModal(); }, []);
  useEffect(() => {
    let alive = true;
    setState({ loading: true });
    getSummary(date).then((data) => { if (alive) setState({ data }); })
      .catch((error) => { if (alive) setState({ error: error.message }); });
    return () => { alive = false; };
  }, [date, revision]);
  const { loading, data, error } = state;
  // 창 바깥(배경)을 누르면 닫는다. 창 안에서 글자를 끌어 선택하다 바깥에서 놓은 경우는 닫지 않는다.
  return <dialog ref={dialog} className="ag-dialog" onCancel={onClose} aria-labelledby="summary-title"
    onMouseDown={(event) => { pressedOutside.current = event.target === event.currentTarget; }}
    onClick={(event) => { if (pressedOutside.current && event.target === event.currentTarget) onClose(); }}>
    <header className="dialog-head">
      <h2 id="summary-title">업무 요약</h2>
      <button type="button" className="dialog-close" onClick={onClose} aria-label="닫기">×</button>
    </header>
    {loading ? <div className="ag-body" role="status">불러오는 중…</div>
      : error ? <div className="ag-body"><p className="form-error" role="alert">{error}</p><button type="button" className="secondary-button" onClick={() => setRevision((n) => n + 1)}>다시 시도</button></div>
      : <SummaryBody data={data} tasks={tasks} onClose={onClose} />}
    <footer className="dialog-actions"><button type="button" className="secondary-button" onClick={onClose}>닫기</button></footer>
  </dialog>;
}

function SummaryBody({ data, tasks, onClose }) {
  const cal = data.calendar || {};
  const counts = data.counts || {};
  const day = `${data.기준일}${cal.요일 ? `(${cal.요일})` : ""}${cal.holiday ? `, ${cal.holiday_name || "공휴일"}` : ""}`;
  return <div className="ag-body">
    <p className="ag-lead">{day} 기준으로 경고 {counts.경고 ?? 0}건, 주의 {counts.주의 ?? 0}건{counts["자료 확인"] ? `, 자료 확인 ${counts["자료 확인"]}건` : ""}입니다.</p>
    {data.items?.length ? <ul className="ag-list">{data.items.map((item) => <AnomalyItem key={item.meter_id} item={item} date={data.기준일} tasks={tasks} onClose={onClose} />)}</ul>
      : <p className="empty-state">{counts.분석 ? "이상징후가 없습니다." : "이 날짜에는 분석 자료가 없습니다."}</p>}
  </div>;
}

// 자료 확인인 까닭(조사까지). 원격검침 값이 그대로인 날과 다시 움직인 첫날은 그렇게 적는다.
const dataReason = (item) => item.reason === "값 그대로" ? `원격검침 값이 ${item.days}일째 그대로라`
  : item.reason === "다시 움직인 첫날" ? "원격검침 값이 다시 움직인 첫날이라" : "관측값에 오류가 있어";

function AnomalyItem({ item, date, tasks, onClose }) {
  const cause = tasks.causes[taskKey(item.meter_id, date)];
  const mail = tasks.mails[taskKey(item.meter_id, date)];
  const dataError = item.likely_data_error || item.심각도 === "자료 확인";
  const tone = dataError ? "unknown" : SEVERITY_TONE[item.심각도] || "unknown";
  function alert() {
    const target = `${withLine(item.역명)}의 ${date} 이상징후`;
    if (window.confirm(mail?.sent ? `${target} 메일을 이미 보냈습니다. 또 보내시겠습니까?` : `${target}를 담당자에게 메일로 알릴까요?`)) tasks.sendMail(item.meter_id, date);
  }
  return <li className="ag-item">
    <div className="ag-item-main">
      <RiskBadge risk={tone} />
      <a className="station-link" href={detailHref(item.meter_id)} onClick={onClose}>{withLine(item.역명)}</a>
      <span>{dataError ? `${dataReason(item)} 위험도를 계산하지 않았습니다.` : <>예측 대비 <strong>{signedPct(item.pct, "비율 없음")}</strong> ({ton(item.error_ton)}톤)</>}</span>
    </div>
    {!dataError && <div className="ag-item-btns">
      <button type="button" className="secondary-button" disabled={cause?.loading || !item.meter_id} onClick={() => tasks.analyze(item.meter_id, date)}>{cause?.loading ? "분석 중…" : cause ? "다시 분석" : "원인 분석"}</button>
      <button type="button" className="secondary-button" disabled={mail?.loading || !item.meter_id} onClick={alert}>{mail?.loading ? "보내는 중…" : "담당자에게 메일"}</button>
    </div>}
    {mail?.loading && <Working text="메일을 보내고 있습니다. 이 창을 닫아도 계속됩니다." />}
    {mail?.message && <p className={mail.ok ? "form-message" : "form-error"} role="status">{mail.message}</p>}
    {cause?.loading && <Working text="원인을 분석하고 있습니다. 이 창을 닫아도 계속됩니다." />}
    {cause && !cause.loading && <CauseResult cause={cause} />}
  </li>;
}

function Working({ text }) {
  return <p className="ag-loading" role="status"><span className="spinner" aria-hidden="true" />{text}</p>;
}

function CauseResult({ cause }) {
  if (cause.error) return <p className="form-error">분석하지 못했습니다. {cause.error}</p>;
  const a = cause.data || {};
  if (a.error) return <p className="form-error">{a.error}</p>;
  const steps = Array.isArray(a.recommendation) ? a.recommendation : a.recommendation ? [a.recommendation] : [];
  return (
    <div className="ag-cause">
      <p><strong>{a.primary_cause}</strong>{a.confidence ? ` (확신도 ${a.confidence})` : ""}</p>
      {a.reasons?.length ? (
        <>
          <p className="ag-cause-label">근거</p>
          <ul>{a.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
        </>
      ) : null}
      {a.events?.length ? (
        <>
          <p className="ag-cause-label">관련 정보</p>
          <ul>
            {a.events.map((e, i) => {
              const url = extractUrl(e.source);
              return (
                <li key={i}>
                  {url ? <a href={url} target="_blank" rel="noreferrer">{e.title || url}</a> : e.title || e.source}
                  {e.date ? ` (${e.date})` : ""}
                </li>
              );
            })}
          </ul>
        </>
      ) : null}
      {steps.length ? (
        <>
          <p className="ag-cause-label">현장에서 먼저 확인할 것</p>
          <ol>{steps.map((s, i) => <li key={i}>{s}</li>)}</ol>
        </>
      ) : null}
      <p className="form-hint">AI가 추정한 내용입니다. 실제 원인은 현장에서 확인하세요.</p>
    </div>
  );
}
