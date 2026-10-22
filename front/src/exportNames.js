// Excel 내려받기 파일명. "종류_조건_기간.xlsx" 형식이며 기간은 파일에 실제로 들어가는 자료의 첫날·마지막 날이다.
// 기간은 서버(/api/export.xlsx 의 export_rows)와 같은 규칙으로 화면이 이미 받은 자료(/api/data)에서 계산한다.
//   사용량: 아리수·일일 수집 계량기의 일 사용량 중 start~end 사이 날짜 → YYMMDD
//   청구내역: 청구월이 start~end 의 달 사이인 청구서 → 청구월만 있으므로 YYMM (날짜를 지어내지 않는다)

const KIND_LABEL = { usage: "사용량", bills: "청구내역" };
const MAX_PART = 30;
const MAX_NAME = 120;

// 2026-01-05 → 260105, 2026-01 → 2601. 형식이 아니면 빈 문자열.
export function compactDate(value) {
  const match = /^\d{2}(\d{2})-(\d{2})(?:-(\d{2}))?$/.exec(String(value || ""));
  return match ? match.slice(1).filter(Boolean).join("") : "";
}

// 운영체제가 파일명에 허용하지 않는 문자와 제어 문자를 지우고, 공백·밑줄을 정리하고, 너무 긴 조각은 자른다.
export function safeFilePart(value) {
  if (value == null || value === false) return "";   // 고르지 않은 조건(false·null)은 빈 조각
  return String(value)
    .replace(/[\\/:*?"<>|\u0000-\u001f\u007f]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/^\.+|\.+$/g, "")
    .slice(0, MAX_PART)
    .trim();
}

export function rangeLabel(range) {
  if (!range?.first || !range?.last) return "";
  const first = compactDate(range.first), last = compactDate(range.last);
  if (!first || !last) return "";
  return first === last ? first : `${first}-${last}`;
}

// kind: usage | bills, parts: 조건 조각(빈 값은 빠진다), range: { first, last } 또는 null(자료 없음)
export function exportFilename(kind, parts = [], range = null) {
  const pieces = [KIND_LABEL[kind] || "내려받기", ...parts.map(safeFilePart)].filter(Boolean);
  const period = rangeLabel(range);
  const unique = pieces.filter((piece, index) => pieces.indexOf(piece) === index);   // 같은 글자가 두 번 들어가지 않게
  let name = [...unique, period || "자료없음"].join("_");
  if (name.length > MAX_NAME) name = [...unique.slice(0, 1), ...unique.slice(1).map((p) => p.slice(0, 12)), period || "자료없음"].join("_");
  return `${name}.xlsx`;
}

// 서버와 같은 규칙으로 고른 계량기. 삭제된 계량기는 /api/data 에 이미 없다.
// meter_ids: 화면이 고른 계량기(검색 결과 등), 쉼표로 이은 문자열이나 배열.
export function exportMeters(meters = [], { office_id, line, meter_id, meter_ids } = {}) {
  const given = (value) => value != null && value !== "" && value !== "all";
  const wanted = given(meter_ids) ? new Set((Array.isArray(meter_ids) ? meter_ids : String(meter_ids).split(",")).map((id) => String(id).trim()).filter(Boolean)) : null;
  return meters.filter((m) => (!given(office_id) || String(m.office_id) === String(office_id))
    && (!given(line) || String(m.line) === String(line))
    && (!given(meter_id) || String(m.id) === String(meter_id))
    && (!wanted || wanted.has(String(m.id))));
}

// 파일에 실제로 들어갈 자료의 첫날·마지막 날. 자료가 없으면 null.
export function exportRange(data, kind, filters = {}) {
  const start = filters.start || "2000-01-01";
  const end = filters.end || "9999-12-31";
  let first = "", last = "";
  const add = (value) => {
    if (!value) return;
    if (!first || value < first) first = value;
    if (!last || value > last) last = value;
  };
  for (const meter of exportMeters(data?.meters, filters)) {
    if (kind === "usage") {
      if (meter.provider !== "arisu" || !meter.daily_enabled) continue;
      for (const row of data?.daily?.[meter.id]?.usage || []) if (row.date >= start && row.date <= end && Number.isFinite(row.value)) add(row.date);
    } else {
      for (const bill of data?.bills?.[meter.id]?.bills || []) {
        const month = bill.ym || String(bill.id || "").split(":")[1] || "";
        if (/^\d{4}-\d{2}$/.test(month) && month >= start.slice(0, 7) && month <= end.slice(0, 7)) add(month);
      }
    }
  }
  return first ? { first, last } : null;
}
