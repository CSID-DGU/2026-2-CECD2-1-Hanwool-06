import { useEffect, useRef, useState } from "react";
import { getCollection, isStatic, startCollection } from "../api.js";
import { collectionLabel, collectionNeedsRefresh, collectionTime, isCollectionActive, isCollectionFinished, meterCollectionState, needsAttention, sortCollectionItems } from "../collection.js";

export function useCollection(userId, onComplete) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const previous = useRef(null);
  const onCompleteRef = useRef(onComplete);
  onCompleteRef.current = onComplete;
  const sessionRef = useRef(userId);
  sessionRef.current = userId;
  useEffect(() => { previous.current = null; setData(null); setError(""); setBusy(false); }, [userId]);
  useEffect(() => {
    if (!userId) return;
    let alive = true, loading = false, timer;
    const load = async () => {
      if (loading) return;
      loading = true;
      clearTimeout(timer);
      try {
        const next = await getCollection();
        if (!alive) return;
        if (collectionNeedsRefresh(previous.current, next)) onCompleteRef.current();
        previous.current = next;
        setData(next); setError("");
      } catch (e) { if (alive && e.status !== 401) setError(e.message); }
      loading = false;
      if (alive) timer = setTimeout(load, isCollectionActive(previous.current?.running) ? 2_000 : 60_000);
    };
    const visible = () => { if (!document.hidden) load(); };
    load(); document.addEventListener("visibilitychange", visible);
    return () => { alive = false; clearTimeout(timer); document.removeEventListener("visibilitychange", visible); };
  }, [userId, revision]);
  const refresh = () => setRevision((n) => n + 1);
  async function start(meterId) {
    setBusy(true); setError("");
    try {
      const job = await startCollection(meterId);
      if (sessionRef.current === userId) {
        previous.current = { ...previous.current, running: job };
        setData((value) => ({ ...value, running: job })); refresh();
      }
      return job;
    } catch (e) { if (sessionRef.current === userId) { setError(e.message); refresh(); } throw e; }
    finally { if (sessionRef.current === userId) setBusy(false); }
  }
  return { data, error, busy, refresh, start };
}

// 저장된 화면에서는 수집을 실행할 수 없으므로 패널을 그리지 않는다.
export default function CollectionPanel(props) {
  return isStatic ? null : <Panel {...props} />;
}

function Panel({ collection, meters = [], meterId, showSettings = false }) {
  const [notice, setNotice] = useState("");
  const [noticeJobId, setNoticeJobId] = useState(null);
  const [actionError, setActionError] = useState("");
  const [expanded, setExpanded] = useState(false);
  useEffect(() => { setNotice(""); setNoticeJobId(null); setActionError(""); setExpanded(false); }, [meterId]);
  useEffect(() => {
    const snapshot = collection?.data;
    const result = [snapshot?.latest, snapshot?.running, ...(snapshot?.history || [])].find((item) => item?.id === noticeJobId && isCollectionFinished(item));
    if (result) { setNotice(""); setNoticeJobId(null); }
  }, [collection?.data, noticeJobId]);
  if (!collection) return null;
  const { data, error, busy } = collection;
  const meter = meterId ? meters.find((m) => String(m.id) === String(meterId)) : null;
  const dailySelected = meter ? meter.provider === "arisu" && Boolean(meter.daily_enabled) : meters.some((m) => m.provider === "arisu" && m.active && !m.deleted_at && m.daily_enabled);
  const meterState = meterId ? data?.meters?.find((m) => String(m.meter_id) === String(meterId)) : null;
  const job = data?.running || data?.latest;
  const active = isCollectionActive(data?.running);
  const meterBusy = isCollectionActive(meterState);
  const meterLabel = meter ? meterCollectionState(meter, meterState) : "";
  const statusLabel = collectionLabel(meterState?.status);
  const canUpdate = meter ? meter.provider === "arisu" && Boolean(meter.active) && !meter.deleted_at : meters.some((m) => m.provider === "arisu" && m.active && !m.deleted_at);
  const running = meterBusy || (!meterId && active);
  async function update() {
    setActionError(""); setNotice(""); setNoticeJobId(null); setExpanded(true);
    try { const next = await collection.start(meterId); setNoticeJobId(next.id); setNotice(next.status === "queued" ? "수집을 예약했습니다." : "수집을 시작했습니다."); }
    catch (e) { setActionError(e.message); }
  }
  const brief = !data ? "수집 상태를 불러오는 중…"
    : meter ? `${meterLabel}${statusLabel !== meterLabel ? `, ${statusLabel}` : ""}, 최근 성공 ${collectionTime(meterState?.last_success_at)}`
    : job ? `최근 작업 ${collectionLabel(job.status)}, ${collectionTime(job.finished_at || job.started_at || job.created_at)}${!active && isCollectionFinished(job) && job.message ? ` (${job.message})` : ""}`
    : "아직 수집한 적이 없습니다.";
  const settings = data?.settings;
  const content = <>
    {!data && !error && <p className="form-hint" role="status">수집 상태를 불러오는 중…</p>}
    {data && <>
      {dailySelected && !data.configured?.water && <p className="collection-note">서버에 아리수 로그인 정보가 없어 일일 사용량은 수집하지 못합니다. 청구서는 수집합니다.</p>}
      {data.worker_enabled === false && <p className="collection-note">서버에서 수집 기능이 꺼져 있습니다. 예약한 작업은 기능을 켜면 처리됩니다.</p>}
      {meter && <div className="collection-meter-summary"><strong>{meterLabel}</strong><span>최근 시도 {collectionTime(meterState?.last_attempt_at)}</span><span>최근 성공 {collectionTime(meterState?.last_success_at)}</span>{statusLabel !== meterLabel && <span>{statusLabel}</span>}{meterState?.message && <p>{meterState.message}</p>}</div>}
      {job && <JobSummary job={job} meters={meters} title={active ? "진행 중인 작업" : "최근 작업"} progress={active} />}
      {!job && <p className="form-hint">아직 수집한 적이 없습니다.</p>}
      <p className="collection-schedule">
        자동 수집 {settings?.enabled ? `매일 ${String(settings.hour).padStart(2, "0")}:00` : "꺼짐"}
        {settings?.enabled && settings.next_run_at ? `, 다음 실행 ${collectionTime(settings.next_run_at)}` : ""}
        <button type="button" className="text-button" onClick={collection.refresh}>상태 새로고침</button>
      </p>
      {showSettings && <p className="form-hint">서버가 켜져 있어야 실행됩니다.</p>}
      {showSettings && data.history?.length > 0 && <details className="collection-history"><summary>최근 수집 이력 {data.history.length}건</summary>{data.history.map((entry) => <JobSummary key={entry.id} job={entry} meters={meters} />)}</details>}
    </>}
    {notice && <p className="form-message" role="status">{notice}</p>}
  </>;
  return <section className={`collection-panel ${showSettings ? "" : "collection-panel--compact"}`} aria-label={meterId ? "계량기 정보 업데이트" : "정보 업데이트"}>
    <div className="collection-heading">
      <div>
        <h2>{meterId ? "계량기 정보 업데이트" : "정보 업데이트"}</h2>
        <p>{showSettings ? "아리수에서 청구서와 일일 사용량을 가져옵니다. 화면의 검색 조건과 상관없이 담당 계량기 전체가 대상입니다." : brief}</p>
      </div>
      <button className="primary-button" type="button" disabled={busy || !canUpdate || running} onClick={update}>{busy ? "요청 중…" : running ? collectionLabel((meterId ? meterState : data?.running)?.status) : meterId ? "정보 업데이트" : "전체 정보 업데이트"}</button>
    </div>
    {!showSettings && <>
      {active && <div className="collection-inline-progress" role="status"><span>{collectionLabel(job.status)} {job.completed || 0}/{job.total || 0}개</span><progress aria-label="수집 진행률" max={Math.max(job.total || 0, 1)} value={job.completed || 0} /></div>}
      <details className="collection-details" open={expanded} onToggle={(e) => setExpanded(e.currentTarget.open)}><summary>수집 내역</summary>{content}</details>
    </>}
    {showSettings && content}
    {(actionError || error) && <p className="form-error" role="alert">{actionError || error} <button className="text-button" type="button" onClick={collection.refresh}>다시 확인</button></p>}
  </section>;
}

function JobSummary({ job, meters, title, progress = false }) {
  return <div className={`collection-job collection-job--${job.status}`}>
    <div className="collection-job-title"><strong>{title ? `${title}: ` : ""}{collectionLabel(job.status)}</strong><span>{job.completed || 0} / {job.total || 0}개</span></div>
    {progress && <progress aria-label="수집 진행률" max={Math.max(job.total || 0, 1)} value={job.completed || 0} />}
    {(job.message || job.status === "queued") && <p>{job.message || "앞 작업이 끝나면 시작합니다."}</p>}
    <small>시작 {collectionTime(job.started_at)}{job.finished_at ? `, 종료 ${collectionTime(job.finished_at)}` : ""}</small>
    {job.items?.length > 0 && <details><summary>계량기별 결과 {job.items.length}건{attentionCount(job.items) ? ` · 실패·확인 필요 ${attentionCount(job.items)}건을 상단에 표시` : ""}</summary><JobItems items={job.items} meters={meters} /></details>}
  </div>;
}

const attentionCount = (items = []) => items.filter((item) => needsAttention(item.status)).length;

// 전체 현황(정보 업데이트)과 계량기 관리의 수집 내역이 함께 쓴다. 성공하지 못한 결과를 위에 둔다(상태값은 그대로).
function JobItems({ items, meters }) {
  return <div className="table-wrap"><table className="data-table"><thead><tr><th>계량기</th><th>결과</th><th>일일 / 청구</th><th>내용</th></tr></thead><tbody>{sortCollectionItems(items).map((item) => {
    const record = meters.find((m) => String(m.id) === String(item.meter_id));
    return <tr key={item.meter_id}><td>{record?.station_name || item.station_name || item.display_name || "계량기"}<small>{record?.customer_number || item.customer_number || item.meter_id}</small></td><td>{collectionLabel(item.status)}</td><td>{item.daily_rows ?? "—"} / {item.bill_rows ?? "—"}</td><td>{item.message || "—"}</td></tr>;
  })}</tbody></table></div>;
}
