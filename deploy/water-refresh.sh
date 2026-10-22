#!/bin/bash
# 승하차 수집 + 모델 갱신. water-refresh.timer가 매일 08:05(KST)에 실행한다.
# 서버 설정에서 자동 수집이 꺼져 있으면(COLLECTION_WORKER_ENABLED) 건너뛰고, 그날 아리수 자동 수집과 재시도(실패분 30분 간격 최대 2번)가 모두 끝날 때까지 1분마다 확인하며 기다린다(최대 4시간).
# 그다음 승하차를 받는데, 전날 자료가 아직 안 올라왔으면 10분마다 다시 받으며 최대 1시간 기다린 뒤 모델을 돌린다.
set -u
PY=/home/water/miniforge3/envs/hanul/bin/python
cd /home/water/app

if [ "$(printf %s "${COLLECTION_WORKER_ENABLED-true}" | tr A-Z a-z)" != "true" ]; then
  echo "서버 설정에서 자동 수집이 꺼져 있어(COLLECTION_WORKER_ENABLED) 건너뜀"; exit 0
fi

for i in $(seq 240); do
  "$PY" - <<'EOF'
import os, sqlite3, sys
from datetime import datetime
from zoneinfo import ZoneInfo
db = sqlite3.connect(f"file:{os.environ['APP_DATA_DIR']}/app.sqlite3?mode=ro", uri=True)
today = datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
settled = db.execute("SELECT value FROM settings WHERE key='collection_auto_settled'").fetchone()
busy = db.execute("SELECT 1 FROM collection_jobs WHERE status IN ('queued','running') LIMIT 1").fetchone()
sys.exit(11 if busy or not settled or settled[0] != today else 0)
EOF
  state=$?
  [ "$state" -ne 11 ] && break
  [ $((i % 10)) -eq 1 ] && echo "오늘 아리수 수집(재시도 포함)이 아직 끝나지 않아 기다리는 중(1분마다 확인)"
  sleep 60
done
case $state in
  0) ;;
  11) echo "4시간 안에 오늘 아리수 수집(재시도 포함)이 끝나지 않아 건너뜀"; exit 0 ;;
  *) echo "설정 확인 실패(코드 $state)"; exit 1 ;;
esac

YESTERDAY=$(TZ=Asia/Seoul date -d yesterday +%F)
for i in $(seq 7); do
  if ! "$PY" -m back.pipelines.refresh --ridership; then
    echo "승하차 수집 실패. 모델 갱신은 계속한다"; break
  fi
  grep -Eq '"status": "(complete|partial)"' "$APP_DATA_DIR/daily/ridership/$YESTERDAY.status.json" 2>/dev/null && break
  if [ "$i" -eq 7 ]; then
    echo "전날($YESTERDAY) 승하차가 1시간 기다려도 없어 그대로 모델을 돌린다(다음 날 다시 받는다)"; break
  fi
  echo "전날($YESTERDAY) 승하차가 아직 올라오지 않아 10분 뒤 다시 받음"
  sleep 600
done
# 지난 두 달 승하차를 월별 파일 값으로 바꾸는 줄. 승하차 기준 수정(1~8호선만 합산)이 배포된 뒤에 아래 두 명령 줄(M1=… 줄과 monthly 줄)의 맨 앞 # 을 지운다.
# 그 전에 켜면 환승역 9곳의 지난 두 달 값이 역 전체 합계(최대 2배)로 바뀌어 달마다 값이 튄다(10-16 교차 검증).
# M1=$(date -d "$(TZ=Asia/Seoul date +%Y-%m-01) -1 month" +%Y-%m); M2=$(date -d "$(TZ=Asia/Seoul date +%Y-%m-01) -2 month" +%Y-%m)
# "$PY" -m back.pipelines.daily_ridership.monthly "$M2" "$M1" || echo "아직 안 올라온 월별 파일이 있음(올라오면 다음 날 바뀐다). 모델 갱신은 계속한다"
"$PY" -m back.pipelines.refresh --model --jobs 1
