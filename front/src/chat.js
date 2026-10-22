import { detailHref, isDetailDate } from "./pages/Detail/data.js";

// 답변을 기다리며 새로 쓴 질문은 실패 복구나 재시도로 덮어쓰지 않는다.
export function chatDraftReducer(draft, action) {
  if (action.type === "edit") return action.value;
  if (action.type === "send") return action.retry && draft.trim() !== action.question ? draft : "";
  if (action.type === "failed") return draft || action.question;
  return draft;
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
