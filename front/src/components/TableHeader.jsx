import Dropdown from "./Dropdown.jsx";
import { splitSortKey } from "../sorting.js";

// 표 머리글 컨트롤. 글자는 다른 머리글과 같은 12px·회색·굵게이고, 정렬 열은 화살표, 조건 열은 ▾가 붙는다.
// 전체 현황의 계량기 목록과 통계의 월별 내역이 함께 쓴다.

// 정렬 머리글: 누르면 onSort(column). 활성 열은 청색이고 ▲(오름)·▼(내림), 나머지는 흐린 ⇅.
export function SortHeader({ column, label, sortKey, onSort, className = "", hints }) {
  const { column: active, direction } = splitSortKey(sortKey);
  const isActive = active === column;
  const ariaSort = isActive ? (direction === "asc" ? "ascending" : "descending") : "none";
  const text = hints || { asc: "오름차순, 누르면 내림차순", desc: "내림차순, 누르면 오름차순", idle: "누르면 정렬" };
  return <th aria-sort={ariaSort} className={`${className} ${isActive ? "is-sorted" : ""}`.trim()}>
    <button type="button" className="th-sort" onClick={() => onSort(column)} title={`${label}: ${isActive ? text[direction] : text.idle}`}>
      {label}<span className="th-sort-mark" aria-hidden="true">{isActive ? (direction === "asc" ? "▲" : "▼") : "⇅"}</span>
    </button>
  </th>;
}

// 조건 머리글: 고르지 않았으면 열 이름만, 골랐으면 고른 값만 보인다. items: [{ value, label }], "all" 이 전체.
export function FilterHeader({ label, value = "all", items, onChange, className = "" }) {
  const chosen = items.find((item) => item.value === value);
  return <th className={`${className} ${value !== "all" ? "is-filtered" : ""}`.trim()}>
    <Dropdown label={label} value={value} display={chosen ? chosen.label : label} active={value !== "all"} buttonClassName="th-dropdown" anchor="parent"
      options={[{ value: "all", label: "전체" }, ...items]} onChange={onChange} />
  </th>;
}
