#!/bin/bash
# 운영 DB와 데이터 폴더 사본을 Object Storage에 올린다. water-backup.timer가 매일 03:30(KST)에 실행한다.
set -euo pipefail
umask 077   # 만든 사본은 water 사용자만 읽을 수 있게
DAY=$(TZ=Asia/Seoul date +%F)
WORK=/var/backups/water-monitor
OUT=$WORK/$DAY
PY=/home/water/miniforge3/envs/hanul/bin/python
S3="aws --endpoint-url $S3_ENDPOINT s3"

rm -rf "$OUT"
mkdir -p "$OUT"
cd /home/water/app
"$PY" -m back.api.manage backup "$OUT/app.sqlite3"   # 실행 중에도 안전한 DB 사본
gzip "$OUT/app.sqlite3"
# DB를 뺀 수집 자료·모델 결과. 읽는 도중 바뀐 파일이 있다는 경고(종료 코드 1)는 실패로 보지 않는다
tar -czf "$OUT/data.tar.gz" -C "$APP_DATA_DIR" --exclude='app.sqlite3*' --exclude='.*.lock' --exclude='./snapshot/versions' . || [ $? -eq 1 ]
# 화면용 스냅샷은 지금 쓰는 버전 하나만 담는다(수집기가 계량기마다 새 버전을 만들어 지난 24시간 버전이 수십 개 쌓인다)
CUR=$("$PY" -c "import json,sys; print(json.load(open(sys.argv[1]))['directory'])" "$APP_DATA_DIR/snapshot/manifest.json" 2>/dev/null || true)
if [ -n "$CUR" ]; then
  tar -czf "$OUT/snapshot.tar.gz" -C "$APP_DATA_DIR" "snapshot/$CUR" || [ $? -eq 1 ]
fi
$S3 cp "$OUT" "s3://$BACKUP_BUCKET/daily/$DAY/" --recursive --only-show-errors

# 서버에는 최근 7일, 스토리지에는 최근 180일(6개월)치 사본만 남긴다. 실제 데이터는 지우지 않는다
find "$WORK" -mindepth 1 -maxdepth 1 -type d -mtime +7 -exec rm -rf {} +
CUTOFF=$(TZ=Asia/Seoul date -d '180 days ago' +%F)
for d in $($S3 ls "s3://$BACKUP_BUCKET/daily/" | awk '{print $2}' | tr -d /); do
  if [[ "$d" < "$CUTOFF" ]]; then
    $S3 rm "s3://$BACKUP_BUCKET/daily/$d/" --recursive --only-show-errors
  fi
done
echo "백업 완료: s3://$BACKUP_BUCKET/daily/$DAY/ ($(du -sh "$OUT" | cut -f1))"
