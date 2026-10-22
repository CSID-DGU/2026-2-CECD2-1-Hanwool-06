import { useState } from "react";
import { changePassword, login } from "../api.js";
import logo from "../assets/seoulmetro.svg";

export function LoginPage({ setupRequired, onLogin, error: connectionError, onRetry }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event) {
    event.preventDefault();
    setBusy(true); setError("");
    try { await onLogin(await login(email.trim(), password)); }
    catch (e) { setError(e.message); }
    finally { setBusy(false); }
  }
  return <main className="auth-page">
    <section className="auth-card">
      <img src={logo} className="auth-logo" alt="서울교통공사" />
      <h1>상수도 이상징후 관제</h1>
      {setupRequired ? <>
        <p>첫 관리자 계정이 아직 없습니다. 서버의 <code>.env</code>에 <code>ADMIN_EMAIL</code>과 <code>ADMIN_PASSWORD</code>를 넣고 서버를 다시 시작하세요.</p>
        <button type="button" className="primary-button" onClick={onRetry}>다시 확인</button>
      </> : <form onSubmit={submit}>
        <label className="form-field">이메일<input type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} /></label>
        <label className="form-field">비밀번호<input type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} /></label>
        {error && <p className="form-error" role="alert">{error}</p>}
        <button className="primary-button" disabled={busy}>{busy ? "로그인 중…" : "로그인"}</button>
        <p className="auth-note">계정이 없거나 비밀번호를 잊었다면 총괄 관리자에게 문의하세요.</p>
      </form>}
      {connectionError && <div className="form-error" role="alert">{connectionError} <button className="text-button" onClick={onRetry}>다시 연결</button></div>}
    </section>
  </main>;
}

export function PasswordPage({ user, onChanged }) {
  const [current, setCurrent] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event) {
    event.preventDefault();
    if (password !== confirm) { setMessage("새 비밀번호 두 칸이 서로 다릅니다."); return; }
    setBusy(true); setMessage("");
    try {
      await changePassword({ current_password: current, new_password: password });
      setCurrent(""); setPassword(""); setConfirm("");
      setMessage("비밀번호를 변경했습니다.");
      await onChanged();
    } catch (e) { setMessage(e.message); }
    finally { setBusy(false); }
  }
  return <main className="management-page"><section className="management-card account-card">
    <h1>내 계정</h1><p>{user.name} ({user.email})</p>
    {user.must_change_password && <p className="data-notice">비밀번호를 새로 정해야 다른 화면을 쓸 수 있습니다.</p>}
    <form onSubmit={submit}>
      <label className="form-field">현재 비밀번호<input type="password" autoComplete="current-password" required value={current} onChange={(e) => setCurrent(e.target.value)} /></label>
      <label className="form-field">새 비밀번호<input type="password" minLength={12} autoComplete="new-password" required value={password} onChange={(e) => setPassword(e.target.value)} /></label>
      <label className="form-field">새 비밀번호 확인<input type="password" minLength={12} autoComplete="new-password" required value={confirm} onChange={(e) => setConfirm(e.target.value)} /></label>
      <p className="form-hint">새 비밀번호는 12자 이상이어야 합니다.</p>
      {message && <p role="status" className="form-message">{message}</p>}
      <button className="primary-button" disabled={busy}>{busy ? "변경 중…" : "비밀번호 변경"}</button>
    </form>
  </section></main>;
}
