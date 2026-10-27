import { useEffect, useReducer, useRef, useState } from "react";
import { getChatStatus, streamChat } from "../api.js";
import { chatDraftReducer, chatSourceHref, chatStageText, chatWebHref } from "../chat.js";
import mulsaemi from "../assets/mulsaemi/mulsaemi.webp";
import mulsaemiProfile from "../assets/mulsaemi/mulsaemi-profile.webp";
import "./agent-chat.css";

function Sources({ sources, onNavigate }) {
  return <footer className="chat-sources">
    <strong>출처</strong>
    {sources.length ? <ul>{sources.map((source, index) => {
      const href = chatSourceHref(source);
      const web = href ? null : chatWebHref(source);
      const label = typeof source?.label === "string" ? source.label : `자료 ${index + 1}`;
      return <li key={`${source?.id || "source"}-${index}`}>
        {href ? <a href={href} onClick={onNavigate}>{label}<span aria-hidden="true"> ↗</span></a>
          : web ? <a href={web} target="_blank" rel="noopener noreferrer">{label}<span className="chat-source-kind"> 웹 검색, {new URL(web).hostname}</span></a>
          : <span>{label}</span>}
        {typeof source?.summary === "string" && source.summary && <p>{source.summary}</p>}
      </li>;
    })}</ul> : <p>조회 근거 없음</p>}
  </footer>;
}

export default function AgentChat() {
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState([]);
  const [conversationId, setConversationId] = useState(null);
  const [draft, updateDraft] = useReducer(chatDraftReducer, "");
  const [pendingQuestion, setPendingQuestion] = useState("");
  const [sending, setSending] = useState(false);
  const [stage, setStage] = useState(null);
  const [error, setError] = useState("");
  const [apiActive, setApiActive] = useState(null);
  const launcher = useRef(null);
  const input = useRef(null);
  const transcript = useRef(null);
  const request = useRef(null);
  const composing = useRef(false);

  useEffect(() => () => request.current?.abort(), []);
  useEffect(() => { if (open) input.current?.focus(); }, [open]);
  useEffect(() => {
    if (!open) return;
    let controller = null;
    async function check() {
      if (controller) return;
      const current = new AbortController();
      controller = current;
      try {
        const result = await getChatStatus(current.signal);
        if (!current.signal.aborted) setApiActive(result?.active === true);
      } catch {
        if (!current.signal.aborted) setApiActive(false);
      } finally {
        if (controller === current) controller = null;
      }
    }
    // 대화 응답과 별개로 API 연결을 확인하고, 닫힌 창에서는 확인 요청을 멈춘다.
    setApiActive(null);
    check();
    const timer = setInterval(check, 60_000);
    return () => { clearInterval(timer); controller?.abort(); };
  }, [open]);
  useEffect(() => {
    if (open && transcript.current) transcript.current.scrollTop = messages.length || pendingQuestion || error ? transcript.current.scrollHeight : 0;
  }, [open, messages, pendingQuestion, sending, error]);

  function close() {
    setOpen(false);
    launcher.current?.focus();
  }
  function navigateToSource() {
    setOpen(false);
    requestAnimationFrame(() => { document.getElementById("main-content")?.focus(); window.scrollTo(0, 0); });
  }
  function startNew() {
    setMessages([]); setConversationId(null); updateDraft({ type: "edit", value: "" }); setPendingQuestion(""); setError("");
    input.current?.focus();
  }
  async function submit(event, retry = false) {
    event?.preventDefault();
    const question = (retry ? pendingQuestion : draft).trim();
    if (!question || request.current) return;
    const controller = new AbortController();
    request.current = controller;
    updateDraft({ type: "send", question, retry });
    setSending(true); setStage(null); setError(""); setPendingQuestion(question);
    try {
      const result = await streamChat(question, conversationId, {
        signal: controller.signal, onProgress: (next) => { if (!controller.signal.aborted) setStage(next); },
      });
      if (controller.signal.aborted) return;
      if (typeof result?.conversation_id !== "string" || !result.conversation_id || typeof result.answer !== "string" || !result.answer.trim() || !Array.isArray(result.sources)) {
        throw new Error("답변 형식을 확인하지 못했습니다. 다시 시도해 주세요.");
      }
      setConversationId(result.conversation_id);
      setMessages((current) => [...current, { role: "user", content: question }, { role: "assistant", content: result.answer, sources: result.sources }]);
      setPendingQuestion("");
    } catch (cause) {
      if (!controller.signal.aborted) {
        setError(cause.message || "답변을 받지 못했습니다. 다시 시도해 주세요.");
        updateDraft({ type: "failed", question });
      }
    } finally {
      if (!controller.signal.aborted) setSending(false);
      if (request.current === controller) request.current = null;
    }
  }

  return <>
    <button ref={launcher} type="button" className={`chat-launcher${open ? " is-open" : ""}`} aria-label={open ? "물샘이 대화 닫기" : "물샘이에게 물어보기"}
      aria-haspopup="dialog" aria-expanded={open} aria-controls={open ? "agent-chat" : undefined} onClick={() => open ? close() : setOpen(true)}>
      <span className="chat-launcher-label" aria-hidden="true">무엇을 도와드릴까요?</span>
      <img className="chat-launcher-mascot" src={mulsaemi} alt="" />
    </button>
    {open && <section id="agent-chat" className="chat-panel" role="dialog" aria-modal="false" aria-labelledby="agent-chat-title"
      onKeyDown={(event) => { if (event.key === "Escape" && !event.nativeEvent.isComposing && !composing.current) { event.preventDefault(); event.stopPropagation(); close(); } }}>
      <header className="chat-header">
        <div className="chat-heading"><img className="chat-mark" src={mulsaemiProfile} alt="" /><div><h2 id="agent-chat-title">물샘이</h2>
          <p className={`chat-connection is-${apiActive == null ? "checking" : apiActive ? "active" : "inactive"}`} role="status" title="답변 서비스 연결 상태">
            <span className="chat-connection-dot" aria-hidden="true" /><span className="sr-only">답변 서비스 연결 상태: </span>{apiActive == null ? "확인 중" : apiActive ? "활성화" : "비활성화"}
          </p></div></div>
        <div className="chat-actions"><button type="button" onClick={startNew} disabled={sending || (!messages.length && !pendingQuestion)} aria-label="새 대화 시작">새 대화</button>
          <button type="button" className="chat-close" onClick={close} aria-label="채팅 닫기">×</button></div>
      </header>
      <div className="chat-transcript" ref={transcript} role="log" aria-label="물샘이 대화" aria-live="polite" aria-relevant="additions" aria-busy={sending} tabIndex={0}>
        {!messages.length && !pendingQuestion && <div className="chat-welcome">
          <h3>어떤 자료를 찾아드릴까요?</h3>
          <p>역명과 날짜를 알려 주시면<br />사용량과 이상 징후를 확인해 드립니다.</p>
          <div className="chat-examples">{["최근 7일 동안 경고가 나온 역을 알려 주세요", "경복궁역은 2025년 1월 1일부터 오늘까지 주의가 몇 번 있었나요?", "석촌역의 최근 7일 사용량을 그 전 7일과 비교해 주세요"].map((question) => <button type="button" key={question} onClick={() => { updateDraft({ type: "edit", value: question }); input.current?.focus(); }}>{question}<span aria-hidden="true">↗</span></button>)}</div>
          <p className="chat-welcome-note">답변 아래 출처에서 날짜별 상세 자료를 확인하세요.</p>
        </div>}
        {messages.map((message, index) => <article key={index} className={`chat-message is-${message.role}`} aria-label={message.role === "user" ? "내 질문" : "물샘이 답변"}>
          <span className="chat-speaker">{message.role === "assistant" && <img className="chat-speaker-avatar" src={mulsaemiProfile} alt="" />}{message.role === "user" ? "나" : "물샘이"}</span>
          <div className="chat-message-body">{message.content}</div>
          {message.role === "assistant" && <Sources sources={message.sources} onNavigate={navigateToSource} />}
        </article>)}
        {pendingQuestion && <article className="chat-message is-user" aria-label="내 질문"><span className="chat-speaker">나</span><div className="chat-message-body">{pendingQuestion}</div></article>}
        {sending && <p className="chat-loading" role="status"><span aria-hidden="true" />{chatStageText(stage)}</p>}
        {error && <div className="chat-error"><p role="alert">{error}</p><button type="button" className="text-button" onClick={(event) => submit(event, true)} disabled={sending || !pendingQuestion}>다시 시도</button><span>실패한 질문을 다시 보내거나 새 질문을 입력하세요.</span></div>}
      </div>
      <form className="chat-composer" onSubmit={submit}>
        <label htmlFor="agent-chat-input" className="sr-only">물샘이에게 질문</label>
        <div className="chat-input-row"><textarea id="agent-chat-input" ref={input} rows={2} value={draft} maxLength={2000} placeholder="역명과 궁금한 내용을 입력하세요" aria-describedby="agent-chat-input-hint"
          onChange={(event) => updateDraft({ type: "edit", value: event.target.value })} onCompositionStart={() => { composing.current = true; }} onCompositionEnd={() => { composing.current = false; }}
          onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing && !composing.current && event.keyCode !== 229) { event.preventDefault(); submit(); } }} />
          <button type="submit" className="chat-send" disabled={sending || !draft.trim()} aria-label="질문 보내기"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M12 19V5M6 11l6-6 6 6" /></svg></button></div>
        <div className="chat-input-hint" id="agent-chat-input-hint"><span>{sending ? "답변을 기다리는 동안 다음 질문을 입력할 수 있습니다." : "Enter 전송 · Shift+Enter 줄바꿈"}</span><span>{draft.length.toLocaleString()} / 2,000</span></div>
      </form>
    </section>}
  </>;
}
