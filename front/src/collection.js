export const isCollectionActive = (job) => ["queued", "running"].includes(job?.status);
export const isCollectionFinished = (job) => ["success", "partial", "failed", "interrupted"].includes(job?.status);
export const collectionLabel = (status) => ({
  unverified: "연결 미확인", queued: "수집 예약", running: "수집 중", success: "수집 완료",
  partial: "일부 수집 완료", failed: "수집 실패", interrupted: "수집 중단", skipped: "수집 제외",
  access_required: "아리수 조회 권한 필요",
  empty: "조회 자료 없음", not_configured: "로그인 설정 미완료", configuration_required: "로그인 설정 미완료", no_data: "조회 자료 없음",
})[status] || "연결 미확인";

// 계량기별 결과에서 먼저 볼 순서. 상태값은 바꾸지 않고 보여 주는 순서만 정한다.
//   0 실패: 수집 실패, 로그인 설정 필요, 서버 중지로 중단 (서버가 재시도·실패 메일 대상으로 보는 상태)
//   1 일부 수집 완료 (서버도 실패로 셈)
//   2 확인 필요: 조회 자료 없음(이 계정으로 볼 수 없는 고객번호일 수 있음), 조회 권한 필요, 알 수 없는 상태
//   3 진행 중: 수집 중, 수집 예약
//   4 수집 제외: 삭제·비활성이라 일부러 뺀 계량기
//   5 수집 완료
const RESULT_RANK = { failed: 0, configuration_required: 0, not_configured: 0, interrupted: 0, partial: 1,
  empty: 2, no_data: 2, access_required: 2, running: 3, queued: 3, skipped: 4, success: 5 };
export const collectionResultRank = (status) => RESULT_RANK[status] ?? 2;
export const needsAttention = (status) => collectionResultRank(status) <= 2;

// 성공하지 못한 결과가 위로 오도록 정렬한 사본. 같은 순위 안에서는 받은 순서(서버의 계량기 순)를 그대로 둔다.
export function sortCollectionItems(items = []) {
  return items.map((item, index) => ({ item, index }))
    .sort((a, b) => collectionResultRank(a.item.status) - collectionResultRank(b.item.status) || a.index - b.index)
    .map(({ item }) => item);
}

// The next queued job may become visible before the previous one is returned as latest.
export function collectionNeedsRefresh(previous, next) {
  if (!previous) return false;
  return Boolean(
    isCollectionActive(previous.running) && previous.running.id !== next.running?.id
    || isCollectionFinished(next.latest) && (next.latest.id !== previous.latest?.id || !isCollectionFinished(previous.latest))
  );
}

export function collectionTime(value) {
  if (!value) return "기록 없음";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "시각 확인 필요";
  return new Intl.DateTimeFormat("ko-KR", { timeZone: "Asia/Seoul", year: "numeric", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(date);
}

export function meterCollectionState(meter, record) {
  if (meter.deleted_at) return "삭제 보관";
  if (meter.provider !== "arisu") return "자동 수집 미지원";
  if (record?.connection_verified) return "연결 확인";
  return "연결 미확인";
}
