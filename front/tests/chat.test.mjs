import test, { after, before } from "node:test";
import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";
import { chatDraftReducer, chatSourceHref } from "../src/chat.js";
import { getChatStatus, sendChat, setCsrfToken } from "../src/api.js";
import { buildStations, detailHref, isDetailDate } from "../src/pages/Detail/data.js";

test("sending clears the composer immediately and a failed question can be restored and retried", () => {
  const question = "석촌역의 가장 최근 사용량을 알려 주세요";
  let draft = chatDraftReducer("", { type: "edit", value: ` ${question} ` });
  draft = chatDraftReducer(draft, { type: "send", question });
  assert.equal(draft, "");
  draft = chatDraftReducer(draft, { type: "failed", question });
  assert.equal(draft, question);
  draft = chatDraftReducer(draft, { type: "send", question, retry: true });
  assert.equal(draft, "");
});

test("a delayed failure and retry preserve the next question already being composed", () => {
  const question = "최근 7일 동안 경고가 나온 역을 알려 주세요";
  const nextQuestion = "경복궁역의 주의 횟수는?";
  let draft = chatDraftReducer(question, { type: "send", question });
  draft = chatDraftReducer(draft, { type: "edit", value: nextQuestion });
  draft = chatDraftReducer(draft, { type: "failed", question });
  assert.equal(draft, nextQuestion);
  draft = chatDraftReducer(draft, { type: "send", question, retry: true });
  assert.equal(draft, nextQuestion);
  draft = chatDraftReducer(draft, { type: "failed", question });
  assert.equal(draft, nextQuestion);
  draft = chatDraftReducer(draft, { type: "send", question: nextQuestion });
  assert.equal(draft, "");
});

test("chat sources link only to the exact server-provided meter and date", () => {
  const source = { meter_id: "meter&a", date: "2026-09-01", href: "#/detail?meter=meter%26a&date=2026-09-01" };
  assert.equal(chatSourceHref(source), source.href);
  assert.equal(chatSourceHref({ ...source, date: null, href: "#/detail?meter=meter%26a" }), "#/detail?meter=meter%26a");
  for (const href of [null, "javascript:alert(1)", "https://example.com/", "//#/detail?meter=meter%26a", "#/users",
    "#/detail?meter=other&date=2026-09-01", "#/detail?meter=meter%26a&date=2026-09-02", "#/detail?meter=meter%26a",
    `${source.href}&meter=other`, `${source.href}&date=2026-09-02`, `${source.href}&next=https://example.com`, `${source.href}#outside`]) {
    assert.equal(chatSourceHref({ ...source, href }), null, String(href));
  }
  assert.equal(chatSourceHref({ ...source, date: "2026-02-30", href: "#/detail?meter=meter%26a&date=2026-02-30" }), null);
  assert.equal(chatSourceHref(null), null);
  assert.equal(chatSourceHref({ ...source, meter_id: null }), null);
});

test("detail links preserve real calendar dates without changing existing meter links", () => {
  assert.equal(detailHref("a&b"), "#/detail?meter=a%26b");
  assert.equal(detailHref("a&b", "2024-02-29"), "#/detail?meter=a%26b&date=2024-02-29");
  for (const value of [null, "2026-02-29", "2026-04-31", "2026-13-01", "2026-9-01", "2026-09-01&meter=other"]) assert.equal(isDetailDate(value), false);
  assert.equal(isDetailDate("2024-02-29"), true);
});

test("chat sends only the question and server conversation ID with session CSRF and cancellation", async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  const response = { conversation_id: "saved-conversation", answer: "확인할 역을 알려 주세요.", sources: [] };
  try {
    globalThis.fetch = async (path, options) => { calls.push({ path, ...options }); return { ok: true, status: 200, json: async () => response }; };
    setCsrfToken("chat-csrf");
    const controller = new AbortController();
    assert.equal(await sendChat("지난달은요?", "previous-conversation", controller.signal), response);
    await sendChat("오늘 현황을 알려 주세요");
    assert.equal(calls[0].path, "/api/chat");
    assert.equal(calls[0].method, "POST");
    assert.equal(calls[0].credentials, "same-origin");
    assert.equal(calls[0].headers["X-CSRF-Token"], "chat-csrf");
    assert.equal(calls[0].signal, controller.signal);
    assert.deepEqual(JSON.parse(calls[0].body), { message: "지난달은요?", conversation_id: "previous-conversation" });
    assert.deepEqual(JSON.parse(calls[1].body), { message: "오늘 현황을 알려 주세요", conversation_id: null });
    globalThis.fetch = async () => ({ ok: false, status: 503, json: async () => ({ detail: "자료 저장소에 연결하지 못했습니다." }) });
    await assert.rejects(sendChat("다시 조회"), (error) => error.status === 503 && error.message === "자료 저장소에 연결하지 못했습니다.");
  } finally { globalThis.fetch = originalFetch; setCsrfToken(); }
});

test("chat connection checks use a cancellable authenticated GET and preserve inactive responses", async () => {
  const originalFetch = globalThis.fetch;
  const controller = new AbortController();
  const calls = [];
  try {
    globalThis.fetch = async (path, options) => {
      calls.push({ path, ...options });
      return { ok: true, status: 200, json: async () => ({ active: false }) };
    };
    assert.deepEqual(await getChatStatus(controller.signal), { active: false });
    assert.equal(calls[0].path, "/api/chat/status");
    assert.equal(calls[0].method, "GET");
    assert.equal(calls[0].credentials, "same-origin");
    assert.equal(calls[0].signal, controller.signal);
    assert.equal(calls[0].body, undefined);
    globalThis.fetch = async (_, { signal }) => new Promise((_, reject) => {
      signal.addEventListener("abort", () => reject(signal.reason), { once: true });
    });
    const pending = getChatStatus(controller.signal);
    controller.abort();
    await assert.rejects(pending, { name: "AbortError" });
  } finally { globalThis.fetch = originalFetch; }
});

const meter = { id: "meter-a", customer_number: "001", station_id: "station", station_name: "테스트역", office_id: "office", line: "1", active: true, daily_enabled: true };
const data = {
  meters: [meter], offices: [{ id: "office", name: "시험 영업사업소" }], stations: {}, bills: {},
  daily: { "meter-a": { usage: [{ date: "2026-09-01", value: 20 }, { date: "2026-10-01", value: 10 }] } },
  risk: { "meter-a": [{ date: "2026-09-01", pred: 10, actual: 20, residual: 10, severity: "경고" }, { date: "2026-10-01", pred: 10, actual: 10, residual: 0, severity: "정상" }] },
};
const errorDay = { date: "2026-09-01", actual: 0, pred: null, residual: null, err: true, reason: "값 그대로", since: "2026-08-01", days: 32, usual: 10 };

test("error-only risk history stays inspectable without becoming a normal verdict", () => {
  const line = buildStations({}, {}, data.daily, { "meter-a": [errorDay] }, [meter])[0].lines[0];
  assert.equal(line.risk, null);
  assert.equal(line.riskError, true);
  assert.deepEqual(line.riskDays.map((day) => [day.date, day.severity, day.pct, day.note.reason]), [["2026-09-01", "자료 확인", null, "값 그대로"]]);
});

let server, DetailPage;
before(async () => {
  server = await createServer({ server: { middlewareMode: true, ws: false, watch: null }, appType: "custom" });
  DetailPage = (await server.ssrLoadModule("/src/pages/Detail/DetailPage.jsx")).default;
});
after(async () => { await server?.close(); });
const detail = (date, snapshot = data) => renderToStaticMarkup(createElement(DetailPage, { data: snapshot, hash: detailHref("meter-a", date) }));

test("date-linked detail shows the requested day, while an ordinary link still shows latest", () => {
  const earlier = detail("2026-09-01");
  assert.match(earlier, /<input[^>]+type="date"[^>]+value="2026-09-01"/);
  assert.match(earlier, /aria-pressed="true" aria-label="9월 1일 경고"/);
  assert.match(earlier, /예측보다 10톤/);
  assert.match(earlier, /2026년 9월/);
  const latest = detail();
  assert.match(latest, /<input[^>]+type="date"[^>]+value="2026-10-01"/);
  assert.match(latest, /aria-pressed="true" aria-label="10월 1일 정상"/);
  assert.match(latest, /사용량이 예측 범위 안에 있습니다/);
});

test("missing dates and error-only dates never fall back to a newer normal analysis", () => {
  const missing = detail("2026-08-15");
  assert.match(missing, /2026-08-15에는 위험도 분석 결과가 없습니다/);
  assert.match(missing, /<input[^>]+type="date"[^>]+value="2026-08-15"/);
  assert.match(missing, /2026년 8월/);
  assert.doesNotMatch(missing, /사용량이 예측 범위 안에 있습니다/);
  const onlyError = detail("2026-09-01", { ...data, risk: { "meter-a": [errorDay] } });
  assert.match(onlyError, /원격검침 값이 그대로라\(0톤\) 판정하지 않았습니다/);
  assert.match(onlyError, /aria-pressed="true" aria-label="9월 1일 자료 확인"/);
  const noAnalysis = detail("2026-09-01", { ...data, risk: {} });
  assert.match(noAnalysis, /2026-09-01에는 위험도 분석 결과가 없습니다/);
  assert.match(noAnalysis, /2026년 9월/);
});
