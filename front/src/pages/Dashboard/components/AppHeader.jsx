export default function AppHeader({ logo, user, hash, refreshing, onRefresh, onLogout, onSummary }) {
  const tab = (href, label, active) => (
    <a className={active ? "is-active" : ""} aria-current={active ? "page" : undefined} href={href}>{label}</a>
  );
  return (
    <header className="topbar">
      <a className="brand" href="#/">
        <img src={logo} alt="서울교통공사" className="brand-logo" />
        <strong>상수도 이상징후 관제</strong>
      </a>
      <nav className="topnav" aria-label="화면 구분">
        {tab("#/", "전체 현황", hash === "#/" || hash === "")}
        {tab("#/detail", "역 상세", hash.startsWith("#/detail"))}
        {tab("#/statistics", "통계", hash.startsWith("#/statistics"))}
        {tab("#/meters", "계량기 관리", hash.startsWith("#/meters"))}
        {user.role === "superadmin" && onLogout && tab("#/users", "사용자 관리", hash.startsWith("#/users"))}
      </nav>
      {onLogout ? <div className="account-actions">
        <button type="button" onClick={onSummary}>업무 요약</button>
        <button type="button" onClick={onRefresh} disabled={refreshing}>{refreshing ? "확인 중…" : "새로고침"}</button>
        <a href="#/account" title={user.email}>{user.name} <span>{user.role === "superadmin" ? "총괄" : "사업소"}</span></a>
        <button type="button" onClick={onLogout}>로그아웃</button>
      </div> : <div className="account-actions"><span>저장된 화면</span></div>}
    </header>
  );
}
