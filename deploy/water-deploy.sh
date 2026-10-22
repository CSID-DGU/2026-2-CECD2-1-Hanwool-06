#!/bin/bash
# GitHub main에서 CI를 통과한 새 커밋을 받아 반영한다. water-deploy.timer가 2분마다 root로 실행한다.
# GitHub은 서버에 접속하지 않는다. /etc/water-deploy.env에 읽기 전용 토큰(이 레포 Contents·Actions 읽기)이 있으면 그것으로 받고 CI 결과를 확인한다. 공개 레포인 지금은 없어도 되고, 비공개로 바꾸면 필요하다.
# 웹을 다시 띄워야 하는 커밋은 수집 작업 중, 03:30 백업·08:05 갱신·14:00 보관이 도는 동안, 07:50~08:05에는 미룬다(재시작하면 수집이 끊긴다).
# git·pip·npm은 water 사용자로 돌린다(저장소에 root 소유 파일이 생기면 다음 배포가 깨진다). 웹 재시작만 root로 한다.
# 재시작 뒤 /api/health가 1분 안에 응답하지 않으면 이전 커밋과 이전 화면 빌드로 되돌리고, 그 커밋은 새 커밋이 올라올 때까지 다시 시도하지 않는다. 패키지 설치는 되돌리지 않는다.
# deploy/(systemd·nginx·이 스크립트)는 root 설정이라 받아만 두고 설치하지 않는다. 바뀌면 로그에 남기니 deploy/README.md대로 직접 설치한다.
# 인자 check를 주면 할 일만 보여 주고 아무것도 바꾸지 않는다: water-deploy.sh check
set -euo pipefail
REPO=CSID-DGU/2026-2-CECD2-1-Hanwool-06
APP=/home/water/app
PY=/home/water/miniforge3/envs/hanul/bin/python
ENVF=/etc/water-monitor.env
STATE=/var/lib/water-deploy
LOCK=/run/water-deploy.lock
# 받기만 하면 되는 파일(문서·테스트·CI·root 설정)과 빌드만 하면 되는 화면 파일. 그 밖의 파일이 바뀌면 웹을 재시작한다
QUIET='^$|^(\.github|deploy|docs|tests)/|\.md$|^\.gitignore$|^\.env\.example$'
FRONT='^front/|^package(-lock)?\.json$'
DEPS='(^|/)requirements[^/]*\.txt$|^environment\.yml$'
CHECK=false; [ "${1:-}" = check ] && CHECK=true

# 2분마다 도는 확인이라 runuser 대신 setpriv로 권한만 바꾼다(runuser는 부를 때마다 PAM 세션 기록을 남긴다)
as_water() { setpriv --reuid=water --regid=water --init-groups env HOME=/home/water "$@"; }
# GitHub 토큰(있을 때만 쓴다). 명령줄 인자에 두면 그 순간 ps로 다른 계정에 보이므로, git에는 export한 환경변수로,
# GitHub API(curl)에는 표준입력 설정(-K -)으로 넘기고 디스크에 쓰지 않는다
TOKENF=/etc/water-deploy.env
TOKEN=$({ sed -n 's/^GITHUB_TOKEN=//p' "$TOKENF" 2>/dev/null || true; } | tail -n1 | sed "s/^['\"]//; s/['\"]\$//")
if [ -n "$TOKEN" ]; then
  export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.https://github.com/.extraheader
  export GIT_CONFIG_VALUE_0="AUTHORIZATION: basic $(printf 'x-access-token:%s' "$TOKEN" | base64 -w0)"
fi
git_() { as_water git -C "$APP" "$@"; }
api() { curl -fsS -m 20 -H 'Accept: application/vnd.github+json' -K - "$1" <<<"${TOKEN:+header = \"Authorization: Bearer $TOKEN\"}"; }
# 웹 설정에서 비밀이 아닌 값 하나만 읽는다. 파일을 통째로 불러오면 비밀값이 pip·npm 환경에 들어간다
env_value() { sed -n "s/^$1=//p" "$ENVF" | tail -n1 | sed "s/^['\"]//; s/['\"]\$//"; }
DATA=$(env_value APP_DATA_DIR); DATA=${DATA:-/var/lib/water-monitor}
HOST=$(env_value APP_ALLOWED_HOSTS | cut -d, -f1)

busy() {   # 지금 웹을 재시작하면 안 되는 이유를 출력한다. 없으면 1
  local unit now rc=0
  systemctl is-active --quiet water-monitor || { echo "water-monitor가 꺼져 있음(점검 중이면 켠 뒤에 반영)"; return 0; }
  for unit in water-backup water-refresh water-storage; do
    case $(systemctl show -p ActiveState --value "$unit.service") in
      activating|active|deactivating|reloading) echo "$unit 실행 중"; return 0 ;;
    esac
  done
  now=$((10#$(TZ=Asia/Seoul date +%H%M)))
  if [ "$now" -ge 750 ] && [ "$now" -lt 805 ]; then echo "08:00 자동 수집 전후"; return 0; fi
  as_water "$PY" - "$DATA/app.sqlite3" <<'EOF' || rc=$?
import sqlite3, sys
db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=30)
sys.exit(10 if db.execute("SELECT 1 FROM collection_jobs WHERE status IN ('queued','running') LIMIT 1").fetchone() else 0)
EOF
  case $rc in
    0) return 1 ;;
    10) echo "수집 작업 진행 중" ;;
    *) echo "운영 DB 확인 실패(코드 $rc)" ;;
  esac
}

healthy() {   # 웹이 다시 뜰 때까지 1분 기다린다
  local i
  for i in $(seq 30); do
    curl -fsS -m 5 -o /dev/null -H "Host: ${HOST:-127.0.0.1}" http://127.0.0.1:8000/api/health 2>/dev/null && return 0
    sleep 2
  done
  return 1
}

note() {   # 같은 커밋의 같은 상태는 로그에 한 번만 남긴다(2분마다 같은 줄이 쌓이지 않게)
  if $CHECK || [ "$(cat "$STATE/status" 2>/dev/null)" != "$target $1" ]; then echo "$2"; fi
  $CHECK || echo "$target $1" > "$STATE/status"
}

swapped=false; restarted=false
fail() {   # 받기 전 커밋(과 화면 빌드)으로 되돌리고, 이 커밋은 새 커밋이 올라올 때까지 다시 시도하지 않는다
  echo "반영 실패: $1. ${prev:0:7}로 되돌림"
  git_ reset --keep --quiet "$prev" || echo "git 되돌리기 실패. 직접 확인 필요"
  if $swapped; then
    as_water bash -c 'cd "$1/front" && if [ -d dist.prev ]; then rm -rf dist.failed && { [ ! -d dist ] || mv dist dist.failed; } && mv dist.prev dist; fi' _ "$APP" \
      || echo "화면 빌드 되돌리기 실패. 직접 확인 필요"
  fi
  if $restarted; then
    systemctl restart water-monitor || true
    if healthy; then echo "이전 버전으로 정상 응답"; else echo "이전 버전도 /api/health 응답 없음. 직접 확인 필요"; fi
  fi
  echo "$target" > "$STATE/failed"
  exit 1
}

exec 9>"$LOCK"
flock -n 9 || { echo "다른 배포가 진행 중이라 건너뜀"; exit 0; }
mkdir -p "$STATE"

git_ fetch --quiet origin main || { echo "GitHub에서 main을 받지 못함"; exit 1; }
prev=$(git_ rev-parse HEAD)
target=$(git_ rev-parse origin/main)
if [ "$prev" = "$target" ]; then
  if $CHECK; then echo "최신 상태(${prev:0:7})"; fi
  exit 0
fi
if [ "$(cat "$STATE/failed" 2>/dev/null)" = "$target" ]; then
  if $CHECK; then echo "${target:0:7}은 반영에 실패해 되돌린 커밋이라 새 커밋을 기다리는 중(journalctl -t water-deploy.sh)"; fi
  exit 0
fi
if ! git_ merge-base --is-ancestor "$prev" "$target"; then
  echo "GitHub main(${target:0:7})이 서버 커밋(${prev:0:7})에서 이어지지 않음(강제 push 등). 직접 확인 필요"
  $CHECK || echo "$target" > "$STATE/failed"
  exit 1
fi

ci=$(api "https://api.github.com/repos/$REPO/actions/workflows/ci.yml/runs?head_sha=$target&event=push&per_page=1" 2>/dev/null |
     jq -r '.workflow_runs[0] | if . == null then "none" elif .status != "completed" then "pending" else .conclusion end') ||
  { note api "GitHub API로 CI 결과를 확인하지 못함(다음 실행에 다시 확인)"; exit 0; }
case $ci in
  success) ;;
  pending) note pending "${target:0:7} CI 진행 중"; exit 0 ;;
  none) note none "${target:0:7} CI 실행 기록이 아직 없음([skip ci] 커밋은 배포하지 않는다)"; exit 0 ;;
  *) note "$ci" "${target:0:7} CI 결과가 $ci라 배포하지 않음"; exit 0 ;;
esac

changed=$(git_ diff --name-only "$prev" "$target")
count() { grep -cE "$@" <<<"$changed" || true; }
restart=false; front=false; deps=false
[ "$(count -v "$QUIET|$FRONT")" -gt 0 ] && restart=true
[ "$(count "$FRONT")" -gt 0 ] && front=true
[ "$(count "$DEPS")" -gt 0 ] && deps=true
if $CHECK; then
  echo "반영할 커밋: ${prev:0:7} → ${target:0:7} (CI 통과). 웹 재시작: $restart, 화면 빌드: $front, 패키지 설치: $deps"
  git_ log --oneline "$prev..$target"
  if $restart && why=$(busy); then echo "지금은 미룸: $why"; else echo "다음 실행(2분 안)에 반영"; fi
  exit 0
fi
if $restart && why=$(busy); then
  note "wait $why" "${target:0:7} 반영을 미룸: $why"
  exit 0
fi

echo "반영 시작: ${prev:0:7} → ${target:0:7}"
git_ log --oneline "$prev..$target"
git_ merge --ff-only --quiet "$target" || {
  echo "git merge 실패(서버에서 직접 고친 파일이 다음 커밋과 겹치는지 확인). 반영하지 않음"
  echo "$target" > "$STATE/failed"; exit 1; }
if $deps; then
  echo "패키지 설치"
  runuser -l water -c "cd '$APP' && '$PY' -m pip install --quiet -r requirements-dev.txt" || fail "패키지 설치 실패"
fi
if $front; then
  # 새 폴더에 빌드한 뒤 바꿔 끼운다. 빌드가 실패해도 지금 화면은 그대로 남는다
  echo "화면 빌드"
  runuser -l water -c "cd '$APP' && npm ci --no-audit --no-fund && rm -rf front/dist.next && npm run build --workspace front -- --outDir dist.next --emptyOutDir" \
    || fail "화면 빌드 실패"
  swapped=true
  as_water bash -c 'cd "$1/front" && rm -rf dist.prev && { [ ! -d dist ] || mv dist dist.prev; } && mv dist.next dist' _ "$APP" \
    || fail "화면 교체 실패"
fi
if $restart; then
  # 받는 사이 수집이 시작됐으면 끝날 때까지 기다린다(자동 수집은 20분쯤 걸린다). 30분이 지나면 그대로 재시작한다
  waited=0
  while why=$(busy); do
    [ "$waited" -gt 0 ] || echo "재시작 전 기다리는 중: $why"
    [ "$waited" -lt 1800 ] || { echo "30분이 지나 그대로 재시작"; break; }
    sleep 30; waited=$((waited + 30))
  done
  restarted=true
  systemctl restart water-monitor || true
  healthy || fail "재시작 뒤 /api/health가 1분 안에 응답하지 않음"
fi
echo "반영 완료: ${target:0:7}$($restart && echo ', 웹 재시작함')$($front && echo ', 화면 새로 빌드함')"
manual=$(grep -E '^deploy/' <<<"$changed" | tr '\n' ' ' || true)
[ -z "$manual" ] || echo "deploy/ 변경은 설치하지 않았음. deploy/README.md '깔거나 고칠 때'대로 확인 명령 두 줄을 먼저 돌린 뒤 설치: $manual"
rm -f "$STATE/failed" "$STATE/status"
