import { useEffect, useRef, useState } from "react";
import { answerReport, createReport, getReports, markReportRead } from "../api.js";
import { collectionTime } from "../collection.js";

// 오류 보고(문의) 목록을 화면이 보이는 동안 1분마다 확인한다.
// 머리글 숫자(pending): 서버 관리자는 답변 대기 건수, 그 밖의 사용자는 아직 열어 보지 않은 답변 건수.
export function useReports(user) {
  const userId = user?.id;
  const [reports, setReports] = useState([]);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => { setReports([]); setError(""); }, [userId]);
  useEffect(() => {
    if (!userId) return;
    let alive = true;
    const load = () => getReports().then((next) => { if (alive) { setReports(next); setError(""); } })
      .catch((e) => { if (alive && e.status !== 401) setError(e.message); });
    const visible = () => { if (!document.hidden) load(); };
    load();
    const timer = setInterval(visible, 60_000);
    document.addEventListener("visibilitychange", visible);
    return () => { alive = false; clearInterval(timer); document.removeEventListener("visibilitychange", visible); };
  }, [userId, revision]);
  const after = (call) => async (...args) => { const result = await call(...args); setRevision((n) => n + 1); return result; };
  const pending = reports.filter((r) => (user?.server_admin ? !r.answer : r.answer && !r.answer_read)).length;
  return { reports, error, pending, create: after(createReport), answer: after(answerReport), markRead: after(markReportRead) };
}

// 상태 표시: 서버 관리자에게는 답변 대기·답변 완료, 보낸 사람에게는 답변 대기·새 답변·답변 완료.
const statusOf = (r, admin) => (!r.answer ? ["waiting", "답변 대기"] : !admin && !r.answer_read ? ["new", "새 답변"] : ["done", "답변 완료"]);

// 보낸 사람: 새 오류 보고를 쓰거나 문의 내역에서 답변을 본다. 서버 관리자: 받은 보고를 열어 답변한다.
export default function ReportPanel({ user, reports, onClose }) {
  const dialog = useRef(null);
  const admin = user.server_admin;
  const [tab, setTab] = useState(admin ? "waiting" : reports.pending ? "history" : "new");
  const [openId, setOpenId] = useState(null);
  const [notice, setNotice] = useState("");
  useEffect(() => { dialog.current.showModal(); }, []);
  const opened = reports.reports.find((r) => r.id === openId);
  const tabs = admin ? [["waiting", `답변 대기 ${reports.pending}`], ["all", `전체 ${reports.reports.length}`]]
    : [["new", "새 오류 보고"], ["history", `문의 내역 ${reports.reports.length}`]];
  const show = (next, message = "") => { setTab(next); setOpenId(null); setNotice(message); };
  return <dialog ref={dialog} className="ag-dialog report-dialog" onCancel={onClose} aria-labelledby="report-title">
    <header className="dialog-head">
      <h2 id="report-title">{admin ? "받은 오류 보고" : "오류 보고"}</h2>
      <button type="button" className="dialog-close" onClick={onClose} aria-label="닫기">×</button>
    </header>
    <div className="ag-body">
      <div className="management-tabs" role="group" aria-label="오류 보고 보기">{tabs.map(([value, label]) =>
        <button key={value} type="button" aria-pressed={tab === value} onClick={() => show(value)}>{label}</button>)}</div>
      {reports.error && <p className="form-error" role="alert">{reports.error}</p>}
      {notice && <p className="form-message" role="status">{notice}</p>}
      {opened ? <ReportDetail key={opened.id} report={opened} user={user} reports={reports} onBack={() => setOpenId(null)} />
        : tab === "new" ? <ReportForm create={reports.create} onSent={() => show("history", "보냈습니다. 서버 관리자가 답변하면 문의 내역에 표시됩니다.")} />
        : <ReportList items={tab === "waiting" ? reports.reports.filter((r) => !r.answer) : reports.reports} admin={admin} onOpen={(id) => { setOpenId(id); setNotice(""); }}
            empty={admin ? (tab === "waiting" ? "답변을 기다리는 보고가 없습니다." : "받은 오류 보고가 없습니다.") : "보낸 오류 보고가 없습니다."} />}
    </div>
  </dialog>;
}

function ReportForm({ create, onSent }) {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit(e) {
    e.preventDefault(); setBusy(true); setError("");
    // 보던 화면 주소를 함께 보내 서버 관리자가 어디서 난 오류인지 알게 한다.
    try { await create({ title, body, page: (window.location.hash || "#/").slice(0, 200) }); onSent(); }
    catch (err) { setError(err.message); setBusy(false); }
  }
  return <form className="report-form" onSubmit={submit}>
    <label className="form-field">제목<input required maxLength={100} value={title} onChange={(e) => setTitle(e.target.value)} placeholder="예: 역 상세 달력 날짜가 하루씩 밀려 보입니다" /></label>
    <label className="form-field">내용<textarea required rows={6} maxLength={2000} value={body} onChange={(e) => setBody(e.target.value)} placeholder="어느 화면에서 무엇을 했을 때 어떻게 보였는지 적어 주세요." /></label>
    <p className="form-hint">지금 보던 화면 주소가 함께 전달됩니다. 답변은 문의 내역에서 볼 수 있습니다.</p>
    {error && <p className="form-error" role="alert">{error}</p>}
    <div className="dialog-actions"><button className="primary-button" disabled={busy || !title.trim() || !body.trim()}>{busy ? "보내는 중…" : "보내기"}</button></div>
  </form>;
}

function ReportList({ items, admin, onOpen, empty }) {
  if (!items.length) return <p className="empty-state">{empty}</p>;
  return <table className="data-table report-table"><caption className="sr-only">오류 보고 목록</caption>
    <thead><tr><th>상태</th><th>제목</th>{admin && <th>보낸 사람</th>}<th>보낸 시각</th></tr></thead>
    <tbody>{items.map((r) => { const [tone, label] = statusOf(r, admin); return <tr key={r.id}>
      <td><span className={`report-status is-${tone}`}>{label}</span></td>
      <td><button type="button" className="text-button" onClick={() => onOpen(r.id)}>{r.title}</button></td>
      {admin && <td>{r.user_name}<small>{r.user_email}</small></td>}
      <td>{collectionTime(r.created_at)}</td>
    </tr>; })}</tbody>
  </table>;
}

function ReportDetail({ report, user, reports, onBack }) {
  const admin = user.server_admin;
  const mine = report.user_id === user.id;
  const [tone, label] = statusOf(report, admin);
  // 보낸 사람이 새 답변을 열면 확인한 것으로 남긴다. 실패해도 다음 확인 때 다시 남긴다.
  useEffect(() => { if (mine && report.answer && !report.answer_read) reports.markRead(report.id).catch(() => {}); }, [report.id, report.answered_at]);
  return <article className="report-detail">
    <button type="button" className="text-button" onClick={onBack}>← 목록으로</button>
    <h3><span className={`report-status is-${tone}`}>{label}</span> {report.title}</h3>
    <p className="report-meta">{admin && `${report.user_name} (${report.user_email}) · `}{collectionTime(report.created_at)}{admin && report.page && ` · 보던 화면 ${report.page}`}</p>
    <p className="report-text">{report.body}</p>
    <section className="report-answer" aria-label="답변">
      <h4>답변</h4>
      {admin ? <AnswerForm report={report} answer={reports.answer} />
        : report.answer ? <><p className="report-meta">서버 관리자 {report.answered_by_name} · {collectionTime(report.answered_at)}</p><p className="report-text">{report.answer}</p></>
        : <p className="form-hint">아직 답변이 없습니다. 서버 관리자가 답변하면 여기에 표시됩니다.</p>}
    </section>
  </article>;
}

function AnswerForm({ report, answer }) {
  const [draft, setDraft] = useState(report.answer || "");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  async function submit(e) {
    e.preventDefault(); setBusy(true); setError(""); setMessage("");
    try { await answer(report.id, draft); setMessage("답변을 저장했습니다. 보낸 사람의 문의 내역에 표시됩니다."); }
    catch (err) { setError(err.message); }
    finally { setBusy(false); }
  }
  return <form className="report-form" onSubmit={submit}>
    {report.answer && <p className="report-meta">{report.answered_by_name} · {collectionTime(report.answered_at)} 저장 · {report.answer_read ? "보낸 사람이 확인함" : "보낸 사람이 아직 확인하지 않음"}</p>}
    <label className="form-field">답변 내용<textarea required rows={5} maxLength={2000} value={draft} onChange={(e) => setDraft(e.target.value)} /></label>
    {error && <p className="form-error" role="alert">{error}</p>}
    {message && <p className="form-message" role="status">{message}</p>}
    <div className="dialog-actions"><button className="primary-button" disabled={busy || !draft.trim()}>{busy ? "저장 중…" : report.answer ? "답변 고치기" : "답변 보내기"}</button></div>
  </form>;
}
