// Session cookies stay in the browser; the CSRF token lives only in memory.
let csrfToken = "";
export const setCsrfToken = (token = "") => { csrfToken = token; };

// 저장된 화면(한 파일로 묶은 정적 HTML)에서는 서버 대신 파일 안에 압축해 둔 자료로 답한다. 바꾸는 요청은 모두 거절한다.
const packed = typeof window !== "undefined" ? window.__STATIC_DATA_GZ__ : undefined;
export const isStatic = Boolean(packed);
const staticReady = isStatic ? unpack(packed) : null;
async function unpack(text) {
  const bytes = Uint8Array.from(atob(text), (c) => c.charCodeAt(0));
  const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"));
  return JSON.parse(await new Response(stream).text());
}
function refuse(message, status) {
  const error = new Error(message);
  error.status = status;
  throw error;
}
async function staticRequest(path, method) {
  const data = await staticReady;
  if (method !== "GET") refuse("저장된 화면이라 바꿀 수 없습니다.", 405);
  const [route, query = ""] = path.split("?");
  if (route === "/health") return { ok: true, setup_required: false };
  if (route === "/auth/me") return { user: { id: 0, email: "", name: "저장된 화면", role: "superadmin", active: true, must_change_password: false, office_ids: [] }, csrf_token: "static" };
  if (route === "/data") return new URLSearchParams(query).get("version") === data.version ? null : data;
  if (route === "/meters") return data.meters;
  if (route === "/users") return [];
  if (route === "/collection") return { settings: { enabled: false }, configured: { water: false, bills: false }, worker_enabled: false, running: null, latest: null, history: [], meters: [] };
  return refuse("저장된 화면에서는 제공하지 않습니다.", 404);
}

export async function request(path, { method = "GET", body, responseType = "json", ...options } = {}) {
  if (isStatic) return staticRequest(path, method);
  const response = await fetch(`/api${path}`, {
    ...options, method, credentials: "same-origin", cache: "no-store",
    headers: {
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...(method !== "GET" ? { "X-CSRF-Token": csrfToken } : {}),
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
  const data = response.status === 204 ? null : response.ok && responseType === "blob"
    ? await response.blob() : await response.json().catch(() => {
      if (response.ok) throw new Error("서버 응답 형식이 올바르지 않습니다. API 경로를 확인하세요.");
      return null;
    });
  if (!response.ok) throw failure(response.status, path, data);
  return data;
}
function failure(status, path, data) {
  if (status === 401 && path !== "/auth/login") {
    csrfToken = "";
    window.dispatchEvent(new Event("session-expired"));
  }
  const detail = data?.detail;
  const message = typeof detail === "string" ? detail : Array.isArray(detail)
    ? detail.map((x) => `${x.loc?.slice(1).join(".") || "입력"}: ${x.msg}`).join(" · ")
    : `요청을 완료하지 못했습니다 (${status}).`;
  const error = new Error(message);
  error.status = status;
  return error;
}
// 물샘이 스트림(text/event-stream)을 읽는다. 진행 단계(progress)는 onProgress로 넘기고, 답(result)이나 오류(error) 사건을 돌려준다.
export async function readChatStream(body, onProgress) {
  if (!body?.getReader) throw new Error("답변을 받지 못했습니다. 다시 시도해 주세요.");
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    let end;
    while ((end = buffer.indexOf("\n\n")) >= 0) {
      const data = buffer.slice(0, end).split("\n").filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trimStart()).join("\n");
      buffer = buffer.slice(end + 2);
      if (!data) continue;
      let event;
      try { event = JSON.parse(data); } catch { throw new Error("답변 형식을 확인하지 못했습니다. 다시 시도해 주세요."); }
      if (event?.type === "progress") onProgress?.(event.stage);
      else if (event?.type === "result" || event?.type === "error") {
        reader.cancel().catch(() => {});
        return event;
      }
    }
    if (done) throw new Error("답변을 끝까지 받지 못했습니다. 다시 시도해 주세요.");
  }
}
// 물샘이 답변: 서버가 보내는 진행 단계를 onProgress로 알리고, 마지막에 검증을 마친 답을 돌려준다.
export async function streamChat(message, conversationId = null, { signal, onProgress } = {}) {
  if (isStatic) refuse("저장된 화면에서는 제공하지 않습니다.", 404);
  const path = "/chat/stream";
  const response = await fetch(`/api${path}`, {
    method: "POST", credentials: "same-origin", cache: "no-store", signal,
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    body: JSON.stringify({ message, conversation_id: conversationId }),
  });
  if (!response.ok) throw failure(response.status, path, await response.json().catch(() => null));
  const event = await readChatStream(response.body, onProgress);
  if (event.type === "error") throw failure(event.status, path, { detail: event.detail });
  return event;
}
export const getHealth = () => request("/health");
export const getMe = () => request("/auth/me");
export const login = (email, password) => request("/auth/login", { method: "POST", body: { email, password } });
export const logout = () => request("/auth/logout", { method: "POST" });
export const changePassword = (body) => request("/auth/password", { method: "POST", body });
// The server answers 204 (null here) while the scoped data still matches `version`.
export const getData = (version) => request(`/data${version ? `?version=${encodeURIComponent(version)}` : ""}`);
export const getUsers = () => request("/users");
export const getMeters = (includeDeleted = false) => request(`/meters${includeDeleted ? "?include_deleted=true" : ""}`);
export const saveUser = (id, body) => request(`/users${id ? `/${encodeURIComponent(id)}` : ""}`, { method: id ? "PATCH" : "POST", body });
export const deleteUser = (id) => request(`/users/${encodeURIComponent(id)}`, { method: "DELETE" });
export const saveMeter = (id, body) => request(`/meters${id ? `/${encodeURIComponent(id)}` : ""}`, { method: id ? "PATCH" : "POST", body });
export const deleteMeter = (id) => request(`/meters/${encodeURIComponent(id)}`, { method: "DELETE" });
export const restoreMeter = (id) => request(`/meters/${encodeURIComponent(id)}/restore`, { method: "POST" });
export const getCollection = () => request("/collection");
export const startCollection = (meter_id) => request("/collection", { method: "POST", body: meter_id ? { meter_id } : {} });
export const getSummary = (date) => request(`/summary?${new URLSearchParams(date ? { date } : {})}`);
export const getChatStatus = (signal) => request("/chat/status", { signal });
export const analyzeCause = (meter_id, date) => request("/analyze", { method: "POST", body: { meter_id, date } });
export const sendAlert = (meter_id, date) => request("/alert", { method: "POST", body: { meter_id, date } });
export const getRiskLabels = (meterId) => request(`/risk-labels?meter_id=${encodeURIComponent(meterId)}`);
export const saveRiskLabel = (body) => request("/risk-labels", { method: "POST", body });
export const getReports = () => request("/reports");
export const createReport = (body) => request("/reports", { method: "POST", body });
export const answerReport = (id, answer) => request(`/reports/${encodeURIComponent(id)}/answer`, { method: "POST", body: { answer } });
export const markReportRead = (id) => request(`/reports/${encodeURIComponent(id)}/read`, { method: "POST" });
export const pdfUrl = (id, download = false) => `/api/bills/${encodeURIComponent(id)}/pdf?download=${download}`;
// 파일 요청에 붙인 표식을 서버가 응답 쿠키로 돌려준다(1 준비됨, 0 만들지 못함). 화면은 그때까지 안내를 띄운다.
// 결과: true 준비됨, false 실패했거나 끝내 오지 않음, null 사용자가 안내를 닫음.
export function waitForDownload(name, timeoutMs, closed) {
  return new Promise((resolve) => {
    const started = Date.now();
    const timer = setInterval(() => {
      const mark = document.cookie.split("; ").find((c) => c.startsWith(`${name}=`));
      if (!mark && !closed() && Date.now() - started <= timeoutMs) return;
      clearInterval(timer);
      document.cookie = `${name}=; Max-Age=0; path=/`;
      resolve(mark ? mark.endsWith("=1") : closed() ? null : false);
    }, 300);
  });
}

export const exportUrl = (params) => `/api/export.xlsx?${new URLSearchParams(Object.entries(params).filter(([, value]) => value != null && value !== "" && value !== "all"))}`;
