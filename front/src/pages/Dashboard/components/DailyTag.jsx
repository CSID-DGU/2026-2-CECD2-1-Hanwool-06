// 일일 사용량까지 보는 계량기(첫 수집 대기 포함) 표시. 청구 확인 보기의 노선도 카드와 계량기 목록이 같이 쓴다.
export default function DailyTag() {
  return <span className="station-card-tag">
    <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M1.5 12 6 7.5l3 3L14.5 5" /></svg>일일 관제
  </span>;
}
