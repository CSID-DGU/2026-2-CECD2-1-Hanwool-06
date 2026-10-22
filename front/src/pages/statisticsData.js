import { chartRange, won } from "./Detail/data.js";
import { exportRange } from "../exportNames.js";

// 기본 조회 기간: 그해 1월 1일부터 오늘(한국 시각)까지. lastCollected(마지막 수집일)가 오늘보다 앞이면 그날까지,
// firstCollected(첫 자료일)가 그해 1월 1일보다 뒤면 그날부터.
export function statisticsPeriod(now = new Date(), lastCollected = null, firstCollected = null) {
  const today = new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit" }).format(now);
  const end = lastCollected && lastCollected < today ? lastCollected : today;
  const yearStart = `${end.slice(0, 4)}-01-01`;
  return { start: firstCollected && firstCollected > yearStart && firstCollected <= end ? firstCollected : yearStart, end };
}

// 자료가 있는 첫날·마지막 날. 일 사용량 날짜와 청구월 1일 중 가장 이른·늦은 날이다(대시보드 기간과 같은 기준).
// 청구서는 화면용 스냅샷이 아니라 DB에서 오고 달 단위로 조회되므로, 스냅샷이 밀린 날에도 그달 청구서를 고를 수 있다.
// 첫날은 실제 자료(일 사용량·청구서)로만 정하고, 모르면 null이다.
export function collectedRange(data) {
  const usage = exportRange(data, "usage"), bills = exportRange(data, "bills");
  const firsts = [usage?.first, bills && `${bills.first}-01`].filter(Boolean).sort();
  const lasts = [usage?.last, data?.status?.latest_usage, bills && `${bills.last}-01`].filter(Boolean).sort();
  return lasts.length ? { first: firsts[0] || null, last: lasts.at(-1) } : null;
}

// 요금 그래프의 y축 숫자를 억·만 단위로 짧게 쓴다. 긴 금액 때문에 숫자 칸이 넓어져 x축이 짧아지지 않게 한다.
// 눈금마다 알맞은 단위를 쓴다: 1억 이상은 소수 한 자리(14.9억), 1만 이상은 정수(8,000만), 그 아래는 원 그대로.
// LineChart와 같은 눈금(위·가운데·아래, 반올림한 값)이 서로 다른 글자가 될 때까지만 소수점을 늘린다.
export function compactWonFormatter(values) {
  const range = chartRange(values);
  if (!range) return won;
  const ticks = [range.hi, (range.lo + range.hi) / 2, range.lo].map((t) => Math.round(t));
  const digits = (n) => ({ minimumFractionDigits: n, maximumFractionDigits: n });
  for (let extra = 0; extra <= 3; extra++) {
    const format = (v) => {
      if (!Number.isFinite(v)) return "—";
      if (Math.abs(v) >= 1e8) return `${(v / 1e8).toLocaleString("ko-KR", digits(1 + extra))}억`;
      if (Math.abs(v) >= 1e4) return `${(v / 1e4).toLocaleString("ko-KR", digits(extra))}만`;
      return won(v);
    };
    if (new Set(ticks.map(format)).size === ticks.length) return format;
  }
  return won;
}
