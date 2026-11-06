import { detailHref, isDetailDate } from "./pages/Detail/data.js";

// 답변을 기다리며 새로 쓴 질문은 실패 복구나 재시도로 덮어쓰지 않는다.
export function chatDraftReducer(draft, action) {
  if (action.type === "edit") return action.value;
  if (action.type === "send") return action.retry && draft.trim() !== action.question ? draft : "";
  if (action.type === "failed") return draft || action.question;
  return draft;
}

// 답변을 기다리는 동안 보여 줄 진행 단계 문구. 서버는 단계 코드만 보내고 문구는 화면이 정한다.
const CHAT_STAGES = {
  classify: "질문 해석 중…",
  plan: "답변 준비 중…",
  station: "역 확인 중…",
  data: "사용량·판정 자료 검색 중…",
  arisu: "아리수 원격검침 확인 중…",
  docs: "점검 문서 검색 중…",
  web: "웹 검색 중…",
  compare: "자료 비교·답변 구성 중…",
  repair: "답변 다시 확인 중…",
};
export const chatStageText = (stage) => (typeof stage === "string" && Object.hasOwn(CHAT_STAGES, stage) ? CHAT_STAGES[stage] : "질문 보내는 중…");

// 답변 본문은 줄 단위로 '- ' 목록과 문단으로 묶고, 판정 단어(배지)와 건수·사용량 숫자(굵게)만 표시한다.
// HTML·링크로 해석하지 않고 글자 조각만 돌려준다. 화면은 이것을 React 텍스트로 그린다.
// '아리수 누수위험'은 판정이 아니라 지표 이름이라 칠하지 않는다(상세 화면도 값에만 색을 쓴다).
const ANSWER_VERDICTS = { 경고: "alert", 주의: "warn", 정상: "ok" };
// 판정 단어는 앞에 한글이 붙으면('비정상') 빼고, '주의해/주의하/주의 깊게' 같은 당부도 뺀다.
// 숫자는 건수·장소·횟수·사용량 단위가 붙은 것만('3개월', 날짜, 호선은 그대로).
const ANSWER_MARKS = /(?<![가-힣])(경고|주의(?!해|하| 깊)|정상)|(?<![\d.,])((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s?(?:건|곳|회|번|개소|개(?!월)|㎥|m³|톤|%))/g;
const ANSWER_ITEM = /^\s*[-•]\s+/;

function answerParts(text) {
  const parts = [];
  let last = 0;
  for (const match of text.matchAll(ANSWER_MARKS)) {
    if (match.index > last) parts.push({ text: text.slice(last, match.index) });
    parts.push(match[1] ? { text: match[1], tone: ANSWER_VERDICTS[match[1]] } : { text: match[2], strong: true });
    last = match.index + match[0].length;
  }
  if (last < text.length) parts.push({ text: text.slice(last) });
  return parts;
}

export function chatAnswerBlocks(answer) {
  const blocks = [];
  let lines = [];
  const flush = () => { if (lines.length) blocks.push({ type: "paragraph", parts: answerParts(lines.join("\n")) }); lines = []; };
  for (const line of String(answer ?? "").split(/\r?\n/)) {
    if (ANSWER_ITEM.test(line)) {
      flush();
      const item = answerParts(line.replace(ANSWER_ITEM, ""));
      const previous = blocks.at(-1);
      if (previous?.type === "list") previous.items.push(item);
      else blocks.push({ type: "list", items: [item] });
    } else if (line.trim()) lines.push(line);
    else flush();
  }
  flush();
  return blocks;
}

// 웹 검색 출처는 따로 연다: 서버가 검색 결과에서 확인한 https 주소(kind "web")만, 계정 정보 없이.
// 답변 본문의 주소는 여전히 링크로 만들지 않는다.
export function chatWebHref(source) {
  if (source?.kind !== "web" || typeof source.url !== "string" || source.url.length > 500) return null;
  let url;
  try { url = new URL(source.url); } catch { return null; }
  if (url.protocol !== "https:" || url.username || url.password || !url.hostname) return null;
  return url.href;
}

// 답변 본문은 링크로 해석하지 않고, 서버 출처와 계량기·날짜가 일치하는 내부 경로만 연다.
export function chatSourceHref(source) {
  if (typeof source?.href !== "string" || !source.href.startsWith("#/detail?") || source.href.slice(1).includes("#")) return null;
  if (typeof source.meter_id !== "string" || !source.meter_id.trim()) return null;
  if (source.date != null && !isDetailDate(source.date)) return null;
  const params = new URLSearchParams(source.href.slice("#/detail?".length));
  if ([...params.keys()].some((key) => key !== "meter" && key !== "date")) return null;
  if (params.getAll("meter").length !== 1 || params.get("meter") !== source.meter_id) return null;
  if (params.getAll("date").length !== (source.date == null ? 0 : 1) || params.get("date") !== (source.date ?? null)) return null;
  return detailHref(source.meter_id, source.date);
}
