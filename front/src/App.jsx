import { useState, useEffect, useLayoutEffect, useRef } from "react";
import Dashboard from "./pages/Dashboard/Dashboard.jsx";
import DetailPage from "./pages/Detail/DetailPage.jsx";
import ManagementPage from "./pages/ManagementPage.jsx";
import StatisticsPage from "./pages/StatisticsPage.jsx";
import SummaryPopup from "./components/SummaryPopup.jsx";
import AgentChat from "./components/AgentChat.jsx";
import { useCollection } from "./components/CollectionPanel.jsx";
import { LoginPage, PasswordPage } from "./components/AuthPanels.jsx";
import AppHeader from "./pages/Dashboard/components/AppHeader.jsx";
import { getData, getHealth, getMe, isStatic, logout, setCsrfToken } from "./api.js";
import logo from "./assets/seoulmetro.svg";
import { version } from "../package.json";
import "./pages/Detail/detail.css";

function SiteFooter() {
  return (
    <footer className="site-footer">
      <div>
        <p>문의 <a href="mailto:ai.hanwool.22@gmail.com">ai.hanwool.22@gmail.com</a></p>
        <p className="footer-legal"><span>Copyright © 2026 동국대학교 종합설계 한울. All rights reserved.</span><span>v{version}</span></p>
      </div>
    </footer>
  );
}

export default function App() {
  const [hash, setHash] = useState(window.location.hash);
  // 다른 화면(전체 현황 계량기 목록·노선도 등)에서 역 상세로 들어오면 맨 위부터 보여 준다. 화면은 해시로만 바뀌어
  // 문서 스크롤이 그대로 남기 때문이다. 역 상세 안에서 계량기만 바꾸는 이동(?meter=)과 다른 탭 이동은 그대로 둔다.
  const route = hash.split("?")[0] || "#/";
  const previousRoute = useRef(route);
  useLayoutEffect(() => {
    if (route !== previousRoute.current && route.startsWith("#/detail")) window.scrollTo(0, 0);
    previousRoute.current = route;
  }, [route]);
  const [session, setSession] = useState(undefined);
  const [setupRequired, setSetupRequired] = useState(false);
  const [data, setData] = useState(null);
  const dataRef = useRef(null);
  dataRef.current = data;
  const [error, setError] = useState("");
  const [refreshKey, setRefreshKey] = useState(0);
  const [refreshing, setRefreshing] = useState(false);
  const [summaryOpen, setSummaryOpen] = useState(false);
  const refresh = () => setRefreshKey((v) => v + 1);
  const collection = useCollection(session && !session.must_change_password ? session.id : null, refresh);

  async function establish(result) {
    const me = result?.user && result?.csrf_token ? result : await getMe();
    setCsrfToken(me.csrf_token);
    setData(null);
    setSession(me.user); setError(""); setSetupRequired(false);
  }
  async function checkSession() {
    setSession(undefined); setError("");
    try {
      const health = await getHealth();
      setSetupRequired(Boolean(health.setup_required));
      if (health.setup_required) { setSession(null); return; }
      await establish(await getMe());
    } catch (e) {
      setSession(null);
      if (e.status !== 401) setError(e.message);
    }
  }
  useEffect(() => {
    const expired = () => { setSession(null); setData(null); setCsrfToken(); setSummaryOpen(false); };
    const change = () => setHash(window.location.hash);
    window.addEventListener("session-expired", expired);
    window.addEventListener("hashchange", change);
    checkSession();
    return () => {
      window.removeEventListener("session-expired", expired);
      window.removeEventListener("hashchange", change);
    };
  }, []);

  useEffect(() => {
    if (!session || session.must_change_password) return;
    let alive = true, running = false;
    const load = async () => {
      if (running) return;
      running = true; setRefreshing(true);
      try { const next = await getData(dataRef.current?.version); if (alive) { if (next) setData(next); setError(""); } }
      catch (e) { if (alive && e.status !== 401) setError(e.message); }
      finally { running = false; if (alive) setRefreshing(false); }
    };
    const visible = () => { if (!document.hidden) load(); };
    load();
    const timer = setInterval(visible, 60_000);
    document.addEventListener("visibilitychange", visible);
    return () => { alive = false; clearInterval(timer); document.removeEventListener("visibilitychange", visible); };
  }, [session, refreshKey]);

  async function signOut() {
    try { await logout(); setSession(null); setData(null); setCsrfToken(); setSummaryOpen(false); }
    catch (e) { setError(e.message); }
  }
  if (session === undefined) return <main className="auth-page"><p role="status">확인 중…</p></main>;
  if (!session) return <><LoginPage setupRequired={setupRequired} onLogin={establish} error={error} onRetry={checkSession} /><SiteFooter /></>;

  return <>
    <a className="skip-link" href="#main-content" onClick={(e) => { e.preventDefault(); document.getElementById("main-content")?.focus(); }}>본문으로 이동</a>
    <AppHeader logo={logo} user={session} hash={hash} refreshing={refreshing} onRefresh={refresh}
      onLogout={isStatic ? undefined : signOut} onSummary={isStatic ? undefined : () => setSummaryOpen(true)} />
    {error && <div className="app-error" role="alert">{error} <button onClick={refresh}>다시 시도</button></div>}
    <div id="main-content" tabIndex={-1}>
      {session.must_change_password || hash.startsWith("#/account") ? <PasswordPage user={session} onChanged={() => establish()} />
      : !data ? <main className="management-page"><p role="status">{error ? "자료를 불러오지 못했습니다." : "불러오는 중…"}</p></main>
      : hash.startsWith("#/users") || hash.startsWith("#/meters") ? <ManagementPage data={data} user={session} kind={hash.startsWith("#/users") ? "users" : "meters"} onSaved={refresh} collection={collection} />
      : hash.startsWith("#/statistics") ? <StatisticsPage data={data} />
      : hash.startsWith("#/detail") ? <DetailPage data={data} hash={hash} collection={collection} />
      : <Dashboard data={data} collection={collection} />}
    </div>
    <SiteFooter />
    {!isStatic && !session.must_change_password && <AgentChat key={session.id} />}
    {summaryOpen && !session.must_change_password && <SummaryPopup date={data?.status?.reference_date} onClose={() => setSummaryOpen(false)} />}
  </>;
}
