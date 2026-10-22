#!/bin/bash
# 팀원 Object Storage 보관(back/storage)을 돌린다: 바뀐 수집·모델 파일을 hanwool-data/prod/에 올린다.
# 운영 DB 백업(backup-db)은 여기서 돌리지 않는다. 채팅의 읽기 키가 prod/ 전체를 읽을 수 있어 DB(비밀번호 해시·세션·전 사업소 자료)가 그 키로 내려받히기 때문이다. DB는 03:30 백업이 별도 버킷에 둔다. 08:05 갱신(water-refresh)이 성공하면 바로, 그리고 water-storage.timer가 매일 14:00(KST)에 실행한다.
# 웹(water-monitor) 안의 자동 훅은 켜지 않는다. 훅은 수집 워커 안에서 시간 제한 없이 돌아서, 스토리지가 막히면 08:00 수집 마무리와 모델 갱신이 몇 시간 밀린다.
# 접두어 prod는 팀원 PC가 쓰는 코드 기본값 mulsaemi와 나눈 것이다. 키는 03:30 백업용 서브 계정 키를 이 작업에만 넘긴다.
# root가 손으로 실행하면 설정을 읽은 뒤 water 사용자로 다시 실행한다(자료 폴더에 root 소유 파일이 생기면 보관과 03:30 백업이 깨진다).
# 인자를 주면 그 명령만 실행한다. 예: water-storage.sh status | plan | selftest | verify
set -euo pipefail
if [ "$(id -u)" -eq 0 ]; then
  set -a; . /etc/water-monitor.env; . /etc/water-backup.env; set +a
  exec runuser -u water -- "$(readlink -f "$0")" "$@"
fi
export OBJECT_STORAGE_ENABLED=true OBJECT_STORAGE_BUCKET=hanwool-data OBJECT_STORAGE_PREFIX=prod
export OBJECT_STORAGE_ACCESS_KEY="$AWS_ACCESS_KEY_ID" OBJECT_STORAGE_SECRET_KEY="$AWS_SECRET_ACCESS_KEY"
PY=/home/water/miniforge3/envs/hanul/bin/python
cd /home/water/app

if [ $# -gt 0 ]; then
  exec "$PY" -m back.storage "$@"
fi
exec "$PY" -m back.storage sync
