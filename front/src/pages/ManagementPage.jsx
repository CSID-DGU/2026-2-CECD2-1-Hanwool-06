import { useEffect, useRef, useState } from "react";
import { deleteMeter, getMeters, getUsers, restoreMeter, saveMeter, saveUser } from "../api.js";
import { detailHref } from "./Detail/data.js";
import CollectionPanel from "../components/CollectionPanel.jsx";
import PasswordField from "../components/PasswordField.jsx";
import { collectionLabel, collectionTime, isCollectionActive, meterCollectionState } from "../collection.js";
import Dropdown, { SelectField } from "../components/Dropdown.jsx";
import { FilterHeader, SortHeader } from "../components/TableHeader.jsx";
import { compareText, nextSortKey, sortRows, splitSortKey } from "../sorting.js";

// 계량기 표 정렬: 역(같은 역은 호선·고객번호 순), 고객번호, 삭제 일시(삭제 보관함). 삭제 일시는 ISO 시각이라 글자 순서가 곧 시간 순서다.
const METER_SORT_DEFAULTS = { name: "asc", customer: "asc", deleted: "desc" };
const METER_COLUMNS = {
  name: { value: (m) => m.station_name, type: "text" },
  customer: { value: (m) => m.customer_number, type: "text" },
  deleted: { value: (m) => m.deleted_at, type: "text" },
};
const meterOrder = (a, b) => compareText(a.station_name, b.station_name) || compareText(a.line, b.line) || compareText(a.customer_number, b.customer_number);
const NO_METER_FILTERS = { line: "all", office: "all", mode: "all", collect: "all", active: "all" };
const dataModeLabel = (m) => (m.data_mode === "daily" ? "일일 사용량, 청구서" : m.data_mode === "pending" ? "첫 수집 대기" : "청구서");
// 수집 상태 열의 조건 값: 아리수가 아니면 자동 수집 미지원, 수집한 적 없으면 연결 미확인, 그 밖에는 최근 수집 결과.
const collectKey = (m, state) => (m.provider !== "arisu" ? "자동 수집 미지원" : state ? collectionLabel(state.status) : "연결 미확인");
const optionsOf = (values) => [...new Set(values.filter(Boolean))].sort(compareText).map((value) => ({ value, label: value }));
// 사용자 표 정렬: 이름(같으면 이메일 순). 조건: 역할·담당 사업소·상태.
const USER_SORT_DEFAULTS = { name: "asc" };
const USER_COLUMNS = { name: { value: (u) => u.name, type: "text" } };
const userOrder = (a, b) => compareText(a.name, b.name) || compareText(a.email, b.email);
const NO_USER_FILTERS = { role: "all", office: "all", active: "all" };
const ROLE_LABELS = { office_admin: "사업소 관리자", superadmin: "총괄 관리자" };
const ACTIVE_CHOICES = [{ value: "active", label: "활성" }, { value: "inactive", label: "비활성" }];

export default function ManagementPage({ data, user, kind, onSaved, collection }) {
  const [users, setUsers] = useState([]);
  const [editor, setEditor] = useState(null);
  const [search, setSearch] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [revision, setRevision] = useState(0);
  const [showDeleted, setShowDeleted] = useState(false);
  const [allMeters, setAllMeters] = useState([]);
  const [archiveLoading, setArchiveLoading] = useState(false);
  const [deleting, setDeleting] = useState(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [meterSort, setMeterSort] = useState("name-asc");
  const [meterFilters, setMeterFilters] = useState(NO_METER_FILTERS);
  const [userSort, setUserSort] = useState("name-asc");
  const [userFilters, setUserFilters] = useState(NO_USER_FILTERS);
  const isUsers = kind === "users";
  const allowed = !isUsers || user.role === "superadmin";
  useEffect(() => {
    setEditor(null); setSearch(""); setMessage(""); setError(""); setShowDeleted(false); setDeleting(null);
    setMeterSort("name-asc"); setMeterFilters(NO_METER_FILTERS); setUserSort("name-asc"); setUserFilters(NO_USER_FILTERS);
  }, [kind]);
  // 등록 계량기와 삭제 보관함은 열이 조금 달라, 오갈 때 정렬·조건을 처음으로 돌린다.
  useEffect(() => { setMeterSort("name-asc"); setMeterFilters(NO_METER_FILTERS); }, [showDeleted]);
  useEffect(() => {
    if (!isUsers || !allowed) return;
    let alive = true;
    getUsers().then((result) => { if (alive) setUsers(Array.isArray(result) ? result : result.users || []); })
      .catch((e) => { if (alive) setError(e.message); });
    return () => { alive = false; };
  }, [isUsers, allowed, revision, data]);
  useEffect(() => {
    if (isUsers || !showDeleted) return;
    let alive = true;
    setArchiveLoading(true);
    getMeters(true).then((result) => { if (alive) { setAllMeters(result); setError(""); } })
      .catch((e) => { if (alive) setError(e.message); })
      .finally(() => { if (alive) setArchiveLoading(false); });
    return () => { alive = false; };
  }, [isUsers, showDeleted, revision, data]);
  if (!allowed) return <main className="management-page"><h1>접근 권한이 없습니다</h1><p>사용자 관리는 총괄 관리자만 사용할 수 있습니다.</p></main>;
  const meterRecords = showDeleted ? allMeters.filter((m) => m.deleted_at) : (data.meters || []).filter((m) => !m.deleted_at);
  const searched = (isUsers ? users : meterRecords).filter((item) => (
    isUsers ? `${item.name} ${item.email}` : `${item.station_name} ${item.customer_number} ${item.display_name || ""} ${item.office_name || ""}`
  ).toLowerCase().includes(search.trim().toLowerCase()));
  const offices = data.offices || [];
  const officeNames = (ids = []) => ids.map((id) => offices.find((o) => String(o.id) === String(id))?.name || "미지정").join(", ");
  // 계량기 표의 머리글 조건과 정렬(전체 현황 계량기 목록과 같은 방식). 선택지는 이 표에 실제로 있는 값만.
  const stateOf = (item) => collection?.data?.meters?.find((m) => String(m.meter_id) === String(item.id));
  const officeLabel = (item) => item.office_name || officeNames([item.office_id]);
  const meterChoices = isUsers ? {} : {
    line: [...new Set(meterRecords.map((m) => String(m.line || "")).filter(Boolean))].sort((a, b) => Number(a) - Number(b)).map((line) => ({ value: line, label: `${line}호선` })),
    office: optionsOf(meterRecords.map(officeLabel)),
    mode: optionsOf(meterRecords.map(dataModeLabel)),
    collect: optionsOf(meterRecords.map((m) => collectKey(m, stateOf(m)))),
    active: ACTIVE_CHOICES,
  };
  // 사용자 표의 머리글 조건. 담당 사업소는 사업소 관리자에게 실제로 맡겨진 사업소만 고를 수 있고,
  // 총괄 관리자(담당 "전체")는 특정 사업소 조건에 걸리지 않는다(역할 조건으로 따로 고른다).
  const userChoices = !isUsers ? {} : {
    role: [...new Set(users.map((u) => u.role).filter(Boolean))].sort((a, b) => compareText(ROLE_LABELS[a] || a, ROLE_LABELS[b] || b)).map((role) => ({ value: role, label: ROLE_LABELS[role] || role })),
    office: [...new Map(users.filter((u) => u.role !== "superadmin").flatMap((u) => (u.office_ids || []).map((id) => [String(id), officeNames([id])]))).entries()]
      .sort((a, b) => compareText(a[1], b[1])).map(([value, label]) => ({ value, label })),
    active: ACTIVE_CHOICES,
  };
  const userPasses = (u) => (userFilters.role === "all" || u.role === userFilters.role)
    && (userFilters.office === "all" || (u.role !== "superadmin" && (u.office_ids || []).some((id) => String(id) === userFilters.office)))
    && (userFilters.active === "all" || (u.active ? "active" : "inactive") === userFilters.active);
  const passes = (item) => (meterFilters.line === "all" || String(item.line || "") === meterFilters.line)
    && (meterFilters.office === "all" || officeLabel(item) === meterFilters.office)
    && (meterFilters.mode === "all" || dataModeLabel(item) === meterFilters.mode)
    && (showDeleted || meterFilters.collect === "all" || collectKey(item, stateOf(item)) === meterFilters.collect)
    && (showDeleted || meterFilters.active === "all" || (item.active ? "active" : "inactive") === meterFilters.active);
  const sortKey = !showDeleted && splitSortKey(meterSort).column === "deleted" ? "name-asc" : meterSort;
  const sorted = splitSortKey(sortKey);
  const records = isUsers ? sortRows(searched.filter(userPasses), userSort, USER_COLUMNS, userOrder) : sortRows(searched.filter(passes), sortKey, METER_COLUMNS, meterOrder);
  const total = isUsers ? users.length : meterRecords.length;
  const unit = isUsers ? "명" : "개";
  const filtering = Boolean(search.trim()) || Object.values(isUsers ? userFilters : meterFilters).some((value) => value !== "all");
  const setFilter = (key) => (value) => setMeterFilters((current) => ({ ...current, [key]: value }));
  const setUserFilter = (key) => (value) => setUserFilters((current) => ({ ...current, [key]: value }));
  const sortBy = (column) => setMeterSort(nextSortKey(sortKey, column, METER_SORT_DEFAULTS));
  const sortUsersBy = (column) => setUserSort(nextSortKey(userSort, column, USER_SORT_DEFAULTS));
  const resetTable = () => { setSearch(""); setMeterFilters(NO_METER_FILTERS); setMeterSort("name-asc"); setUserFilters(NO_USER_FILTERS); setUserSort("name-asc"); };
  const DELETED_HINTS = { asc: "오래된 순, 누르면 최근 순", desc: "최근 순, 누르면 오래된 순", idle: "누르면 최근 순" };
  async function saved(result) {
    setEditor(null); setMessage(result?.created ? "등록했습니다. 아리수 계량기는 곧 연결을 확인합니다." : "저장했습니다."); setRevision((n) => n + 1); onSaved(); collection?.refresh();
  }
  async function updateMeter(item) {
    setError(""); setMessage("");
    try { await collection.start(item.id); setMessage(`${item.station_name}(${item.customer_number}) 정보 업데이트를 요청했습니다.`); }
    catch (e) { setError(e.message); }
  }
  async function changeDeleted(item, remove) {
    setActionBusy(true); setError("");
    try {
      if (remove) await deleteMeter(item.id); else await restoreMeter(item.id);
      setDeleting(null); setEditor(null); setRevision((n) => n + 1); onSaved(); collection?.refresh();
      setMessage(remove ? `${item.station_name}(${item.customer_number}) 계량기를 삭제했습니다. 이력은 삭제 보관함에 남습니다.` : `${item.station_name}(${item.customer_number}) 계량기를 복원했습니다.`);
    } catch (e) { setError(e.message); }
    finally { setActionBusy(false); }
  }
  return <main className="management-page">
    <div className="management-heading"><div><h1>{isUsers ? "사용자 관리" : "계량기 관리"}</h1>
      {isUsers && <p>사업소마다 활성 담당자를 2명까지 둘 수 있습니다.</p>}</div>
      <button className="primary-button" type="button" onClick={() => { setEditor({}); setMessage(""); }}>{isUsers ? "사용자 추가" : "계량기 추가"}</button></div>
    {error && <p className="form-error" role="alert">{error} <button onClick={() => setRevision((n) => n + 1)}>다시 시도</button></p>}
    {message && <p className="form-message" role="status">{message}</p>}
    {!isUsers && <CollectionPanel collection={collection} meters={data.meters || []} showSettings />}
    <section className="management-card">
      {!isUsers && <div className="management-tabs" role="group" aria-label="계량기 보관 상태"><button aria-pressed={!showDeleted} onClick={() => setShowDeleted(false)}>등록 계량기</button><button aria-pressed={showDeleted} onClick={() => setShowDeleted(true)}>삭제 보관함</button>{showDeleted && <p>복원하면 다시 관제와 수집 대상이 됩니다.</p>}</div>}
      <div className="table-toolbar">
        <label className="form-field table-search">{isUsers ? "이름·이메일 검색" : "역명·고객번호·사업소 검색"}<input type="search" value={search} onChange={(e) => setSearch(e.target.value)} autoComplete="off" /></label>
        <p className="form-hint" aria-live="polite">{filtering ? `${records.length}${unit} (전체 ${total}${unit} 중)` : `${total}${unit}`}. {isUsers ? "머리글에서 이름은 정렬, 역할·담당 사업소·상태는 조건을 고를 수 있습니다."
          : `머리글에서 역·고객번호${showDeleted ? "·삭제 일시" : ""}는 정렬, 호선·사업소·제공 자료${showDeleted ? "" : "·수집 상태·상태"}는 조건을 고를 수 있습니다.`}</p>
        {filtering && <button type="button" className="text-button" onClick={resetTable}>조건 초기화</button>}
      </div>
      <div className="table-wrap table-wrap--fixed"><table className={`data-table data-table--controls ${isUsers ? "user-admin-table" : "meter-admin-table"}`}><caption className="sr-only">{isUsers ? "사용자 목록" : "계량기 목록"}</caption>
        <colgroup>{(isUsers ? [null, 150, 260, 110, 110] : [null, 180, 150, 140, 210, 90, 230]).map((width, i) => <col key={i} style={width ? { width: `${width}px` } : undefined} />)}</colgroup>
        <thead><tr>{isUsers ? <>
          <SortHeader column="name" label="이름 / 이메일" sortKey={userSort} onSort={sortUsersBy} />
          <FilterHeader label="역할" value={userFilters.role} items={userChoices.role} onChange={setUserFilter("role")} />
          <FilterHeader label="담당 사업소" value={userFilters.office} items={userChoices.office} onChange={setUserFilter("office")} />
          <FilterHeader label="상태" value={userFilters.active} items={userChoices.active} onChange={setUserFilter("active")} />
          <th>관리</th>
        </> : <>
          {/* 역 정렬과 호선 선택을 한 머리글에: 칸 글자가 "역명 / 호선"이기 때문 */}
          <th aria-sort={sorted.column === "name" ? (sorted.direction === "asc" ? "ascending" : "descending") : "none"}
            className={`${sorted.column === "name" ? "is-sorted" : ""} ${meterFilters.line !== "all" ? "is-filtered" : ""}`.trim()}>
            <span className="th-pair">
              <button type="button" className="th-sort" onClick={() => sortBy("name")} title="역 정렬">역<span className="th-sort-mark" aria-hidden="true">{sorted.column === "name" ? (sorted.direction === "asc" ? "▲" : "▼") : "⇅"}</span></button>
              <span aria-hidden="true">/</span>
              <Dropdown label="호선" value={meterFilters.line} display={meterChoices.line.find((o) => o.value === meterFilters.line)?.label || "호선"} active={meterFilters.line !== "all"}
                buttonClassName="th-dropdown" anchor="parent" options={[{ value: "all", label: "전체" }, ...meterChoices.line]} onChange={setFilter("line")} />
            </span>
          </th>
          <SortHeader column="customer" label="계량기 / 고객번호" sortKey={sortKey} onSort={sortBy} />
          <FilterHeader label="사업소" value={meterFilters.office} items={meterChoices.office} onChange={setFilter("office")} />
          <FilterHeader label="제공 자료" value={meterFilters.mode} items={meterChoices.mode} onChange={setFilter("mode")} />
          {showDeleted ? <SortHeader column="deleted" label="삭제 일시" sortKey={sortKey} onSort={sortBy} hints={DELETED_HINTS} />
            : <FilterHeader label="수집 상태" value={meterFilters.collect} items={meterChoices.collect} onChange={setFilter("collect")} />}
          {showDeleted ? <th>상태</th> : <FilterHeader label="상태" value={meterFilters.active} items={meterChoices.active} onChange={setFilter("active")} />}
          <th>관리</th>
        </>}</tr></thead>
        <tbody>{records.map((item) => { const state = isUsers ? null : stateOf(item); return <tr key={item.id}>
          {isUsers ? <><td><strong>{item.name}</strong><small>{item.email}</small></td><td>{item.role === "superadmin" ? "총괄 관리자" : "사업소 관리자"}</td><td>{item.role === "superadmin" ? "전체" : officeNames(item.office_ids)}</td></>
          : <><td>{item.deleted_at || !item.active ? <strong>{item.station_name}</strong> : <a className="station-link" href={detailHref(item.id)}>{item.station_name}</a>}{item.line && <small>{item.line}호선</small>}</td><td>{item.display_name || "기본 계량기"}<small>{item.customer_number}</small></td><td>{item.office_name || officeNames([item.office_id])}</td><td>{item.data_mode === "daily" ? "일일 사용량, 청구서" : item.data_mode === "pending" ? "첫 수집 대기" : "청구서"}</td><td className="meter-collection-cell">{item.deleted_at ? <span>삭제 {collectionTime(item.deleted_at)}</span> : <><strong>{meterCollectionState(item, state)}</strong>{item.provider === "arisu" && <><small>최근 성공 {collectionTime(state?.last_success_at)}</small>{state && <details><summary>{collectionLabel(state.status)}</summary><p>최근 시도 {collectionTime(state.last_attempt_at)}{state.message ? `. ${state.message}` : ""}</p></details>}</>}</>}</td></>}
          <td><span className={`status-label ${!item.active || item.deleted_at ? "status-label--off" : ""}`}>{item.deleted_at ? "삭제됨" : !item.active ? "비활성" : "활성"}</span></td>
          <td><div className="table-actions">{item.deleted_at ? <button type="button" className="secondary-button" disabled={actionBusy} onClick={() => changeDeleted(item, false)}>복원</button> : <>
            <button type="button" className="secondary-button" onClick={() => setEditor(item)} aria-label={`${item.name || item.station_name} 수정`}>수정</button>
            {!isUsers && <><button type="button" className="secondary-button" disabled={collection?.busy || isCollectionActive(state) || !item.active || item.provider !== "arisu"} onClick={() => updateMeter(item)}>{isCollectionActive(state) ? collectionLabel(state.status) : "정보 업데이트"}</button><button type="button" className="text-button danger-text" disabled={actionBusy} onClick={() => { setError(""); setDeleting(item); }} aria-label={`${item.station_name} ${item.customer_number} 삭제`}>삭제</button></>}
          </>}</div></td>
        </tr>; })}</tbody>
      </table>
      {!records.length && <p className="empty-state">{archiveLoading ? "삭제 보관함을 불러오는 중…" : filtering ? <>검색어·조건에 맞는 {isUsers ? "사용자가" : "계량기가"} 없습니다. <button type="button" className="text-button" onClick={resetTable}>조건 초기화</button></> : search ? "검색 조건에 맞는 항목이 없습니다." : showDeleted ? "삭제 보관된 계량기가 없습니다." : "등록된 항목이 없습니다."}</p>}
      </div>
      {!isUsers && <p className="form-hint">비활성 계량기는 관제 목록에 나오지 않습니다. 이력은 그대로 남습니다.</p>}
    </section>
    {editor && <EditorDialog key={`${kind}-${editor.id || "new"}`} title={`${isUsers ? "사용자" : "계량기"} ${editor.id ? "수정" : "추가"}`} onClose={() => setEditor(null)}>
      {isUsers ? <UserForm item={editor} offices={offices} onSaved={saved} /> : <MeterForm item={editor} data={data} onSaved={saved} canPosition={user.role === "superadmin"} onExisting={(item) => { setEditor(null); if (item.deleted_at) changeDeleted(item, false); else if (item.active && item.provider === "arisu") updateMeter(item); else setEditor(item); }} />}
    </EditorDialog>}
    {deleting && <EditorDialog title="계량기 삭제" onClose={() => { if (!actionBusy) setDeleting(null); }}><div className="editor-form"><p><strong>{deleting.station_name}{deleting.line ? ` ${deleting.line}호선` : ""}</strong><br />{deleting.display_name}<br />고객번호 {deleting.customer_number}</p><p className="form-hint">관제 목록과 수집 대상에서 빠집니다. 청구·사용량 이력은 남아 있어 삭제 보관함에서 복원할 수 있습니다.</p>{error && <p className="form-error" role="alert">{error}</p>}<div className="dialog-actions"><button className="secondary-button" disabled={actionBusy} onClick={() => setDeleting(null)}>취소</button><button className="danger-button" disabled={actionBusy} onClick={() => changeDeleted(deleting, true)}>{actionBusy ? "삭제 중…" : "삭제"}</button></div></div></EditorDialog>}
  </main>;
}

function EditorDialog({ title, children, onClose }) {
  const ref = useRef(null);
  useEffect(() => { ref.current.showModal(); }, []);
  return <dialog ref={ref} className="editor-dialog" onCancel={(e) => { e.preventDefault(); onClose(); }} aria-labelledby="editor-title">
    <header className="dialog-head"><h2 id="editor-title">{title}</h2><button type="button" className="dialog-close" onClick={onClose} aria-label="닫기">×</button></header>
    {children}
  </dialog>;
}

function UserForm({ item, offices, onSaved }) {
  const [form, setForm] = useState({ email: "", name: "", role: "office_admin", office_ids: [], active: true, ...item, password: "" });
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const field = (key, value) => setForm((f) => ({ ...f, [key]: value }));
  // 서버 정책과 같은 최소 12자. 확인 칸은 비밀번호를 넣을 때만 맞아야 한다(수정 화면에서 비워 두면 비밀번호를 바꾸지 않는다).
  const mismatch = (form.password || confirm) && confirm !== form.password;
  const passwordError = form.password.length > 0 && form.password.length < 12 ? "비밀번호는 12자 이상이어야 합니다." : "";
  const confirmError = confirm && mismatch ? "비밀번호 확인이 일치하지 않습니다." : "";
  async function submit(e) {
    e.preventDefault(); setBusy(true); setError("");
    try {
      if (passwordError) throw new Error(passwordError);
      if (mismatch) throw new Error("비밀번호와 비밀번호 확인이 서로 다릅니다. 같은 값을 다시 입력하세요.");
      const body = { email: form.email.trim(), name: form.name.trim(), role: form.role, office_ids: form.role === "superadmin" ? [] : form.office_ids, active: form.active };
      if (form.password) body.password = form.password;
      if (form.role === "office_admin" && !form.office_ids.length) throw new Error("담당 사업소를 하나 이상 선택하세요.");
      await saveUser(item.id, body); await onSaved();
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  }
  return <form onSubmit={submit} className="editor-form">
    <label className="form-field">이름<input autoFocus required maxLength={80} value={form.name} onChange={(e) => field("name", e.target.value)} /></label>
    <label className="form-field">이메일<input type="email" required autoComplete="off" value={form.email} onChange={(e) => field("email", e.target.value)} /></label>
    <PasswordField label={item.id ? "비밀번호 초기화 (변경할 때만 입력)" : "초기 비밀번호"} required={!item.id} minLength={12} maxLength={128} autoComplete="new-password"
      value={form.password} onChange={(e) => field("password", e.target.value)} error={passwordError} />
    <PasswordField label="비밀번호 확인" required={Boolean(form.password)} minLength={12} maxLength={128} autoComplete="new-password"
      value={confirm} onChange={(e) => setConfirm(e.target.value)} error={confirmError} />
    <SelectField label="역할" value={form.role} options={[{ value: "office_admin", label: "사업소 관리자" }, { value: "superadmin", label: "총괄 관리자" }]} onChange={(value) => field("role", value)} />
    {form.role === "office_admin" && <fieldset><legend>담당 사업소</legend><div className="office-checkboxes">{offices.map((office) => <label key={office.id} className="check-field"><input type="checkbox" checked={form.office_ids.some((id) => String(id) === String(office.id))} onChange={(e) => field("office_ids", e.target.checked ? [...form.office_ids, office.id] : form.office_ids.filter((id) => String(id) !== String(office.id)))} />{office.name}</label>)}</div></fieldset>}
    <label className="check-field"><input type="checkbox" checked={form.active} onChange={(e) => field("active", e.target.checked)} />활성 계정</label>
    {error && <p className="form-error" role="alert">{error}</p>}
    <button className="primary-button" disabled={busy}>{busy ? "저장 중…" : "저장"}</button>
  </form>;
}

function MeterForm({ item, data, onSaved, canPosition, onExisting }) {
  const stations = [...new Map((data.meters || []).map((m) => [String(m.station_id), { id: m.station_id, name: m.station_name }])).values()].filter((s) => s.id);
  const [isNewStation, setIsNewStation] = useState(!stations.length);
  const [form, setForm] = useState({ provider: "arisu", customer_number: "", station_id: stations[0]?.id || "", station_name: "", line: "1", office_id: data.offices?.[0]?.id || "", display_name: "", address: "", purpose: "", tariff: "", active: true, daily_enabled: false, ...item });
  const [meterNumber, setMeterNumber] = useState(item.metadata?.계량기번호 || "");
  const [arisuCustomerName, setArisuCustomerName] = useState(item.metadata?.arisu_customer_name || "");
  const [position, setPosition] = useState(null);
  const [pickPosition, setPickPosition] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [duplicate, setDuplicate] = useState(null);
  const field = (key, value) => setForm((f) => ({ ...f, [key]: value }));
  async function submit(e) {
    e.preventDefault(); setBusy(true); setError(""); setDuplicate(null);
    try {
      if (form.provider === "arisu" && (!arisuCustomerName.trim() || arisuCustomerName.includes("*"))) throw new Error("별표(*)로 가려지지 않은 아리수 고지서상 성명을 입력하세요.");
      // 역·담당 사업소 선택 칸은 브라우저 기본 선택 상자가 아니라 required 로 막을 수 없으므로 여기서 확인한다.
      if (!isNewStation && !form.station_id) throw new Error("역을 선택하세요.");
      if (!form.office_id) throw new Error("담당 사업소를 선택하세요.");
      const body = {
        provider: form.provider, customer_number: form.customer_number.trim(),
        station_name: isNewStation ? form.station_name.trim() : stations.find((s) => String(s.id) === String(form.station_id))?.name || form.station_name,
        line: Number(form.line), office_id: form.office_id, display_name: form.display_name.trim(),
        purpose: form.purpose.trim(), tariff: form.tariff.trim(), address: form.address.trim(),
        active: form.active,
        daily_enabled: form.provider === "arisu" && Boolean(form.daily_enabled),
        metadata: { 계량기번호: meterNumber.trim(), ...(form.provider === "arisu" ? { arisu_customer_name: arisuCustomerName.trim() } : {}) },
      };
      body.station_id = isNewStation ? "" : form.station_id;
      if (position) {
        if (position.x === "" || position.y === "") throw new Error("지도 X·Y 위치를 모두 입력하세요.");
        body.map_x = Number(position.x); body.map_y = Number(position.y);
      }
      const meter = await saveMeter(item.id, body); await onSaved({ meter, created: !item.id });
    } catch (e) {
      setError(e.message);
      if (e.status === 409 && !item.id) {
        const existing = await getMeters(true).catch(() => data.meters || []);
        setDuplicate(existing.find((m) => m.provider === form.provider && m.customer_number === form.customer_number.trim()) || null);
      }
    }
    finally { setBusy(false); }
  }
  return <form className="editor-form" onSubmit={submit}>
    <SelectField label="공급기관" disabled={Boolean(item.id)} value={form.provider} options={[{ value: "arisu", label: "서울 아리수" }, { value: "other", label: "기타 공급기관" }]}
      onChange={(value) => { field("provider", value); if (value !== "arisu") field("daily_enabled", false); }} />
    <label className="form-field">고객번호<input autoFocus required readOnly={Boolean(item.id)} pattern={form.provider === "arisu" ? "[0-9]{9}" : undefined} maxLength={100} inputMode={form.provider === "arisu" ? "numeric" : "text"} value={form.customer_number} onChange={(e) => field("customer_number", e.target.value)} /><small>{form.provider === "arisu" ? "앞자리 0을 포함한 9자리" : "공급기관의 계약번호"}</small></label>
    {form.provider === "arisu" && <>
      <label className="form-field">아리수 고지서상 성명<input required maxLength={100} pattern="[^*]*" title="별표(*)로 가려지지 않은 고지서상 성명을 입력하세요." value={arisuCustomerName} onChange={(e) => setArisuCustomerName(e.target.value)} /><small>고지서에 적힌 그대로 입력하세요. 별표(*)로 가려진 이름은 쓸 수 없습니다.</small></label>
      <SelectField label="수집할 자료" value={form.daily_enabled ? "daily" : "bills"} options={[{ value: "bills", label: "청구서만" }, { value: "daily", label: "일일 사용량 및 청구서" }]} onChange={(value) => field("daily_enabled", value === "daily")} />
    </>}
    {item.id && <p className="form-hint">고객번호가 바뀌었다면 새 계량기로 등록하세요.</p>}
    <label className="check-field"><input type="checkbox" checked={isNewStation} onChange={(e) => { setIsNewStation(e.target.checked); setPosition(null); }} />목록에 없는 새 역</label>
    {isNewStation ? <label className="form-field">역명<input required value={form.station_name} onChange={(e) => field("station_name", e.target.value)} placeholder="예: 동대문역" /></label>
      : <SelectField label="역" value={String(form.station_id ?? "")} options={stations.map((s) => ({ value: String(s.id), label: s.name }))} searchable searchPlaceholder="역명 검색" emptyText="일치하는 역이 없습니다."
        onChange={(value) => { field("station_id", value); setPosition(null); }} />}
    <div className="form-grid"><SelectField label="호선" value={String(form.line ?? "")} options={[1,2,3,4,5,6,7,8].map((line) => ({ value: String(line), label: `${line}호선` }))} onChange={(value) => field("line", value)} />
      <SelectField label="담당 사업소" value={String(form.office_id ?? "")} options={(data.offices || []).map((o) => ({ value: String(o.id), label: o.name }))} onChange={(value) => field("office_id", value)} /></div>
    <label className="form-field">계량기 이름 / 설치 위치<input required maxLength={100} value={form.display_name} onChange={(e) => field("display_name", e.target.value)} placeholder="예: 대합실 화장실" /></label>
    <label className="form-field">계량기번호<input value={meterNumber} onChange={(e) => setMeterNumber(e.target.value)} /></label>
    <label className="form-field">주소<input value={form.address} onChange={(e) => field("address", e.target.value)} /></label>
    <div className="form-grid"><label className="form-field">용도<input value={form.purpose} onChange={(e) => field("purpose", e.target.value)} /></label><label className="form-field">요금 업종<input value={form.tariff} onChange={(e) => field("tariff", e.target.value)} /></label></div>
    <label className="check-field"><input type="checkbox" checked={form.active} onChange={(e) => field("active", e.target.checked)} />활성 계량기</label>
    <p className="form-hint">{form.provider === "arisu" ? "새로 등록하면 아리수 연결을 바로 확인합니다." : "기타 공급기관 계량기는 자동으로 수집하지 않습니다."}{isNewStation ? " 새 역은 위치를 지정하기 전까지 노선도에 표시되지 않습니다." : ""}</p>
    {canPosition && <fieldset><legend>노선도 위치</legend><p className="form-hint">같은 역의 계량기는 위치를 함께 씁니다.</p><button type="button" className="secondary-button" onClick={() => setPickPosition((v) => !v)}>{pickPosition ? "노선도 닫기" : "노선도에서 고르기"}</button>
      {pickPosition && <div className="map-picker" onClick={(e) => { const r = e.currentTarget.getBoundingClientRect(); setPosition({ x: ((e.clientX - r.left) / r.width * 100).toFixed(2), y: ((e.clientY - r.top) / r.height * 100).toFixed(2) }); }}><img src="/metro_map_rectangle.png" alt="위치 지정용 노선도. 키보드 사용 시 아래 X·Y 입력을 사용하세요." />{position && <span className="map-picker-dot" style={{ left: `${position.x}%`, top: `${position.y}%` }} />}</div>}
      <div className="form-grid"><label className="form-field">가로 X (%)<input type="number" min="0" max="100" step="0.01" value={position?.x ?? ""} placeholder={String(item.map_x ?? "")} onChange={(e) => setPosition((p) => ({ x: e.target.value, y: p?.y ?? "" }))} /></label><label className="form-field">세로 Y (%)<input type="number" min="0" max="100" step="0.01" value={position?.y ?? ""} placeholder={String(item.map_y ?? "")} onChange={(e) => setPosition((p) => ({ x: p?.x ?? "", y: e.target.value }))} /></label></div>
      {position && <button type="button" className="text-button" onClick={() => setPosition(null)}>위치 그대로 두기</button>}</fieldset>}
    {error && <p className="form-error" role="alert">{error}</p>}
    {duplicate && <div className="duplicate-meter"><p>{duplicate.station_name}({duplicate.customer_number}) 계량기가 이미 {duplicate.deleted_at ? "삭제 보관함에 있습니다." : "등록되어 있습니다."}</p><button type="button" className="secondary-button" disabled={busy} onClick={() => onExisting(duplicate)}>{duplicate.deleted_at ? "복원하기" : duplicate.active && duplicate.provider === "arisu" ? "기존 계량기 정보 업데이트" : "기존 계량기 수정"}</button></div>}
    <button className="primary-button" disabled={busy}>{busy ? "저장 중…" : "저장"}</button>
  </form>;
}
