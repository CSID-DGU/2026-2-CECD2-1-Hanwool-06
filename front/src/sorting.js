// 표 정렬 공통 규칙. 정렬 키는 "<열>-asc" | "<열>-desc" 문자열이다.
// 값이 없는 행(null·빈 값·날짜 형식 오류)은 오름차순이든 내림차순이든 항상 뒤로 간다. 원본 배열은 바꾸지 않는다.
const collator = new Intl.Collator("ko", { numeric: true, sensitivity: "base" });
export const compareText = (a, b) => collator.compare(String(a ?? ""), String(b ?? ""));

// "YYYY-MM" 또는 "YYYY-MM-DD" → UTC ms. 달력에 없는 날짜(13월, 2월 30일)는 null.
export function dateValue(raw) {
  if (!raw) return null;
  const match = /^(\d{4})-(\d{2})(?:-(\d{2}))?$/.exec(String(raw));
  if (!match) return null;
  const [year, month, day] = [Number(match[1]), Number(match[2]), Number(match[3] || 1)];
  const time = Date.UTC(year, month - 1, day);
  const date = new Date(time);
  if (!Number.isFinite(time) || date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) return null;
  return time;
}

export const numberValue = (raw) => (typeof raw === "number" && Number.isFinite(raw) ? raw : null);

export function splitSortKey(sortKey, fallback = "") {
  const [column = "", direction = "asc"] = String(sortKey || fallback).split("-");
  return { column, direction: direction === "desc" ? "desc" : "asc" };
}

// 머리글을 눌렀을 때 다음 정렬 키. 같은 열이면 방향을 뒤집고, 다른 열이면 그 열의 기본 방향(defaults[column], 없으면 asc)으로 간다.
export function nextSortKey(current, column, defaults = {}) {
  const { column: key, direction } = splitSortKey(current);
  if (key === column) return `${column}-${direction === "asc" ? "desc" : "asc"}`;
  return `${column}-${defaults[column] || "asc"}`;
}

// columns: { <열>: { value: (row) => any, type: "text" | "number" | "date" } }
// tieBreak: 같은 값일 때 쓰는 비교 함수(없으면 원래 순서 유지)
export function sortRows(rows, sortKey, columns, tieBreak = () => 0) {
  const { column, direction } = splitSortKey(sortKey);
  const spec = columns[column];
  if (!spec) return [...rows];
  const sign = direction === "desc" ? -1 : 1;
  const read = (row) => {
    const raw = spec.value(row);
    if (spec.type === "date") return dateValue(raw);
    if (spec.type === "number") return numberValue(raw);
    return raw == null || raw === "" ? null : String(raw);
  };
  return [...rows].sort((a, b) => {
    const va = read(a), vb = read(b);
    if (va == null && vb == null) return tieBreak(a, b);
    if (va == null) return 1;
    if (vb == null) return -1;
    const result = spec.type === "text" ? compareText(va, vb) : va - vb;
    return result * sign || tieBreak(a, b);
  });
}
