import { useRef, useState } from "react";
import { isStatic, request, waitForDownload } from "../api.js";

// Check session first, then let the browser handle the authenticated file URL.
// saveAs: 서버가 응답 헤더로 붙이는 파일명 대신 filename 으로 저장한다. 브라우저는 Content-Disposition 의 이름이 있으면
// <a download> 이름을 무시하므로, 파일을 받아 메모리에서 꺼내 그 이름으로 저장한다(파일 내용은 서버가 만든 그대로).
export default function FileLink({ href, filename, preview = false, saveAs = false, title, children, className = "secondary-button" }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const turn = useRef(0);   // 요청 번호. 안내를 닫거나 다시 누르면 바뀌어, 지난 요청은 화면을 건드리지 못한다
  async function open(event) {
    if (event.metaKey || event.ctrlKey || event.shiftKey) return;
    event.preventDefault();
    if (busy) return;
    if (isStatic) { setError("저장된 화면에서는 내려받을 수 없습니다."); return; }
    const tab = preview ? window.open("", "_blank") : null;
    if (tab) tab.opener = null;
    const mine = ++turn.current;
    const current = () => turn.current === mine;
    setBusy(true); setError("");
    if (saveAs && !preview) {
      try {
        const blob = await request(href.replace(/^\/api/, ""), { responseType: "blob" });
        if (!current()) return;   // 받는 사이 안내를 닫았으면 저장하지 않는다
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url; link.download = filename;
        document.body.append(link); link.click(); link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 60_000);
      } catch (e) { if (current()) setError(e.message); }
      finally { if (current()) setBusy(false); }
      return;
    }
    const token = Math.random().toString(36).slice(2, 12);
    const url = `${href}${href.includes("?") ? "&" : "?"}dl=${token}`;
    try {
      await request("/auth/me");
      if (!current()) { tab?.close(); return; }   // 확인하는 사이 안내를 닫았으면 받지 않는다
      if (preview && tab) tab.location.href = url;
      else {
        const link = document.createElement("a");
        link.href = url;
        if (preview) { link.target = "_blank"; link.rel = "noreferrer"; }
        else link.download = filename;
        document.body.append(link); link.click(); link.remove();
      }
      const ready = await waitForDownload(`dl_${token}`, 180_000, () => !current());
      if (ready === false && current()) setError("파일을 받지 못했습니다. 잠시 후 다시 눌러 주세요.");
    } catch (e) { tab?.close(); if (current()) setError(e.message); }
    finally { if (current()) setBusy(false); }
  }
  return <span className="file-action">
    <a className={className} href={href} download={preview ? undefined : filename} onClick={open} aria-disabled={busy} title={title} target={preview ? "_blank" : undefined} rel={preview ? "noreferrer" : undefined}>{busy ? "문서 준비 중…" : children}</a>
    {error && <span className="file-error" role="alert">{error}</span>}
    {busy && <div className="download-notice" role="status" aria-live="polite"><div>
      <span className="spinner" aria-hidden="true" />
      {preview ? "문서를 만들고 있습니다." : "파일을 만들고 있습니다."}
      <small>자료가 많으면 몇십 초 걸릴 수 있습니다. 다 되면 {preview ? "새 탭에 열립니다." : "브라우저가 저장합니다."}</small>
      <button type="button" className="text-button" onClick={() => { turn.current += 1; setBusy(false); }}>닫기</button>
    </div></div>}
  </span>;
}
