import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

// 브라우저 기본 <select> 대신 쓰는 드롭다운. 펼친 목록의 여백·글꼴·색을 우리 스타일로 그린다.
// 목록은 문서 맨 위(body)에 띄우므로 스크롤 상자(표 머리글 등) 안에서도 잘리지 않는다. 모달 대화상자 안에서 열면 그 대화상자 안에 띄운다
// (대화상자 바깥은 가려지고 눌리지 않기 때문).
// options: [{ value, label, keywords? }]  value: 현재 값  onChange(value)  display: 버튼에 보일 글자(없으면 고른 선택지의 label)
// buttonClassName: 버튼 모양은 쓰는 곳이 정한다(머리글은 th-dropdown, 입력 양식은 dd-field).  active: 강조 표시 여부
// anchor: "button"(기본)이면 버튼 아래에, "parent"면 버튼을 감싼 칸(표 머리글 칸 등)의 왼쪽 끝·폭에 맞춰 목록을 띄운다
// searchable: 목록 위에 검색칸을 두고 label·keywords 로 선택지를 걸러 보여 준다(계량기처럼 선택지가 많을 때)
// disabled: 고칠 수 없는 칸(계량기 수정의 공급기관 등). labelledBy: 바깥 라벨의 id(입력 양식에서 SelectField 가 넘긴다)
// matchWidth: 목록 폭을 버튼 폭과 똑같이 맞춘다(입력 양식). 긴 선택지는 줄을 바꿔 보인다
export default function Dropdown({ options, value, onChange, display, label, buttonClassName = "", active = false, align = "left", anchor = "button",
  searchable = false, searchPlaceholder = "검색", emptyText = "일치하는 항목이 없습니다.", disabled = false, labelledBy, matchWidth = false }) {
  const [open, setOpen] = useState(false);
  const [cursor, setCursor] = useState(0);
  const [query, setQuery] = useState("");
  const [position, setPosition] = useState(null);
  const [host, setHost] = useState(null);   // 목록을 붙일 곳: 버튼을 감싼 대화상자, 없으면 body
  const button = useRef(null);
  const list = useRef(null);
  const search = useRef(null);
  const id = useId();
  const chosen = options.find((option) => option.value === value);
  const text = display ?? chosen?.label ?? label ?? "";
  const term = query.trim().toLowerCase();
  const shown = term ? options.filter((option) => `${option.label} ${option.keywords || ""}`.toLowerCase().includes(term)) : options;

  const close = (focusButton = true) => { setOpen(false); setQuery(""); if (focusButton) button.current?.focus(); };
  const choose = (option) => { if (option.value !== value) onChange(option.value); close(); };
  const openList = (startAt) => {
    const index = startAt ?? Math.max(0, options.findIndex((option) => option.value === value));
    setHost(button.current?.closest("dialog") || document.body);
    setCursor(index); setQuery(""); setOpen(true);
  };

  // 버튼(또는 감싼 칸) 바로 아래에 목록을 둔다(공간이 없으면 위). 창 크기나 스크롤이 바뀌면 닫는다: 위치를 쫓아가는 것보다 단순하고 어긋나지 않는다.
  useLayoutEffect(() => {
    if (!open) return;
    const rect = (anchor === "parent" ? button.current.parentElement : button.current).getBoundingClientRect();
    const estimated = Math.min(options.length, 10) * 36 + (searchable ? 54 : 10);
    const below = window.innerHeight - rect.bottom >= estimated || rect.top < estimated;
    const gap = anchor === "parent" ? 0 : 4;
    setPosition({ top: below ? rect.bottom + gap : null, bottom: below ? null : window.innerHeight - rect.top + gap,
                  left: align === "right" ? null : rect.left, right: align === "right" ? window.innerWidth - rect.right : null, minWidth: rect.width });
    (searchable ? search.current : list.current)?.focus();
    const panel = () => list.current?.parentElement;
    const onScroll = (event) => { if (!panel()?.contains(event.target)) close(false); };
    const onPointer = (event) => { if (!panel()?.contains(event.target) && !button.current?.contains(event.target)) close(false); };
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onScroll);
    document.addEventListener("pointerdown", onPointer);
    return () => { window.removeEventListener("scroll", onScroll, true); window.removeEventListener("resize", onScroll); document.removeEventListener("pointerdown", onPointer); };
  }, [open, options.length, align, anchor, searchable]);

  useEffect(() => {
    if (!open) return;
    list.current?.children[cursor]?.scrollIntoView({ block: "nearest" });
  }, [open, cursor]);
  useEffect(() => { setCursor(0); }, [term]);

  const onButtonKey = (event) => {
    if (["ArrowDown", "ArrowUp", "Enter", " "].includes(event.key)) { event.preventDefault(); openList(event.key === "ArrowUp" ? options.length - 1 : undefined); }
  };
  const onListKey = (event) => {
    const last = shown.length - 1;
    if (event.key === "Escape" || (event.key === "Tab" && !searchable)) { event.preventDefault(); close(); }
    else if (event.key === "ArrowDown") { event.preventDefault(); setCursor((i) => Math.min(last, i + 1)); }
    else if (event.key === "ArrowUp") { event.preventDefault(); setCursor((i) => Math.max(0, i - 1)); }
    else if (event.key === "Home") { event.preventDefault(); setCursor(0); }
    else if (event.key === "End") { event.preventDefault(); setCursor(last); }
    else if (event.key === "Enter" || (event.key === " " && !searchable)) { event.preventDefault(); if (shown[cursor]) choose(shown[cursor]); }
    else if (event.key === "Tab") close();
  };

  const listId = `${id}-list`;
  return <>
    <button ref={button} type="button" className={`dd-button ${buttonClassName} ${active ? "is-active" : ""}`} aria-haspopup="listbox" aria-expanded={open} disabled={disabled}
      aria-controls={open ? listId : undefined} aria-label={labelledBy ? undefined : label ? `${label}: ${text}` : undefined} aria-labelledby={labelledBy ? `${labelledBy} ${id}-value` : undefined}
      title={label} onClick={() => (open ? close() : openList())} onKeyDown={onButtonKey}>
      <span className="dd-button-text" id={`${id}-value`}>{text}</span><span className="dd-mark" aria-hidden="true" />
    </button>
    {open && position && host && createPortal(
      <div className={`dd-panel ${searchable ? "dd-panel--search" : ""}`} style={{ top: position.top ?? undefined, bottom: position.bottom ?? undefined, left: position.left ?? undefined, right: position.right ?? undefined, minWidth: position.minWidth, width: matchWidth ? position.minWidth : undefined }} onKeyDown={onListKey}>
        {searchable && <input ref={search} className="dd-search" type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder={searchPlaceholder}
          aria-label={`${label || ""} 검색`} aria-controls={listId} aria-activedescendant={shown[cursor] ? `${id}-${cursor}` : undefined} autoComplete="off" />}
        <ul ref={list} id={listId} className="dd-list" role="listbox" tabIndex={-1} aria-label={label} aria-activedescendant={searchable ? undefined : `${id}-${cursor}`}>
          {shown.map((option, index) => (
            <li key={option.value} id={`${id}-${index}`} role="option" aria-selected={option.value === value}
              className={`dd-option ${index === cursor ? "is-cursor" : ""}`}
              onPointerMove={() => setCursor(index)} onClick={() => choose(option)}>
              {option.label}
            </li>
          ))}
          {!shown.length && <li className="dd-empty" aria-live="polite">{emptyText}</li>}
        </ul>
      </div>, host)}
  </>;
}

// 입력 양식의 선택 칸. 다른 입력칸과 같은 모양의 버튼(dd-field)에 표 머리글과 같은 목록을 쓴다.
// options: [{ value, label }]. 선택지가 많으면(searchable) 목록 위에 검색칸이 붙는다.
export function SelectField({ label, value, options, onChange, className = "", ...dropdown }) {
  const id = useId();
  return <div className={`form-field ${className}`.trim()}>
    <span id={id}>{label}</span>
    <Dropdown label={label} labelledBy={id} value={value} options={options} onChange={onChange} buttonClassName="dd-field" matchWidth {...dropdown} />
  </div>;
}
