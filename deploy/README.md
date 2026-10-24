# 운영 서버 설정 (`deploy/`)

NCP 운영 서버(`water-prod`, Ubuntu 24.04)에 실제로 깔려 있는 설정 파일을 그대로 모았습니다. 비밀값이 든 설정 파일(`/etc/water-monitor.env`, `/etc/water-backup.env`)은 깃에 올리지 않고, 항목 이름과 형식만 `*.env.example`에 둡니다.

## 매일 도는 순서

| 시각(KST) | 무엇이 | 하는 일 |
|---|---|---|
| 00:00, 12:00, 18:00 | 웹 서버(`water-monitor.service`) 안의 수집기 | 아리수 누수위험·계량기 상태만 확인(스마트검침 카드와 일별내역의 날짜별 상태). 사용량·청구서·모델은 건드리지 않음. 08:00 확인은 아래 전체 수집이 같이 함 |
| 03:30 | `water-backup.timer` → `water-backup.sh` | DB 사본 + 데이터 폴더(이력 자료 `history/` 포함) + 지금 화면용 스냅샷을 묶어 Object Storage `hanwool-water-backup/daily/<날짜>/`에 올림. 서버에 7일, 스토리지에 180일 보관 |
| 08:00 | 웹 서버(`water-monitor.service`) 안의 수집기 | 아리수 일일 사용량·청구서 수집. 일시 오류는 30분 간격 최대 2번 재시도, 남은 실패는 총괄 관리자에게 메일. 다 끝나면 `settings.collection_auto_settled`에 날짜를 남김 |
| 08:05~ | `water-refresh.timer` → `water-refresh.sh` | 위 표시가 생길 때까지 1분마다 확인 → 승하차 수집(전날 자료가 없으면 10분마다 다시, 최대 1시간) → 모델 갱신 → 성공하면 아래 보관을 바로 한 번 더 실행(`OnSuccess`. 채팅이 버킷 사본을 읽어서, 이게 없으면 12:15 보관까지 채팅이 화면보다 하루 늦음) |
| 00:15, 12:15, 14:00, 18:15 | `water-storage.timer` → `water-storage.sh` | 팀원 Object Storage 보관(`back/storage`): 바뀐 수집·모델 파일을 `hanwool-data/prod/`에 올림. 00:15·12:15·18:15는 아리수 상태 확인 15분 뒤라 채팅이 그 확인값을 바로 봄(08시 확인분은 위 `OnSuccess`가 올림). 화면용 스냅샷은 지금 버전만 남김. 운영 DB는 올리지 않음(채팅 읽기 키가 `prod/`를 읽으므로. DB는 03:30 백업 버킷에만). 웹 안의 자동 훅은 켜지 않음(아래 '함께 알아 둘 것') |

## 자동 배포 (GitHub → 서버)

`main`에 push하면 GitHub Actions CI(`.github/workflows/ci.yml`)가 파이썬 테스트·화면 테스트·화면 빌드를 돌립니다. 서버의 `water-deploy.timer`가 2분마다 `water-deploy.sh`를 실행해, `main`에 새 커밋이 있고 그 커밋의 CI가 통과했으면 받아서 반영합니다. GitHub은 서버에 접속하지 않습니다. 레포가 공개라 토큰 없이 받습니다. 레포를 비공개로 바꾸면 서버의 `/etc/water-deploy.env`에 읽기 전용 토큰(이 레포의 Contents·Actions 읽기만 허용한 fine-grained PAT)을 두면 그것으로 받고 CI 결과를 확인합니다. 토큰은 root만 읽고, git에는 명령줄이 아니라 환경변수로 넘깁니다.

| 바뀐 파일 | 반영 방법 |
|---|---|
| `back/` 등 코드 | `water-monitor` 재시작 → `/api/health` 확인 |
| `requirements*.txt`, `back/**/requirements.txt`, `environment.yml` | 위에 더해 `pip install -r requirements-dev.txt` |
| `front/`, `package.json`, `package-lock.json` | `npm ci` → 새 폴더(`front/dist.next`)에 빌드 → `front/dist`와 바꿔 끼움. 재시작 없이 바로 보임 |
| `*.md`, `docs/`, `tests/`, `.github/` | 받기만 함 |
| `deploy/` | 받기만 함. root 설정이라 아래 '깔거나 고칠 때'대로 직접 설치(로그에 알림) |

- 재시작이 필요한 커밋은 웹이 꺼져 있을 때, 수집 작업 중, 03:30 백업·08:05 갱신·보관(00:15·12:15·14:00·18:15)이 도는 동안, 07:50~08:05에는 미뤘다가 다음 실행에 반영합니다(재시작하면 수집이 끊김).
- 재시작 뒤 1분 안에 `/api/health`가 응답하지 않으면 이전 커밋과 이전 화면 빌드로 되돌리고 다시 재시작합니다. 그 커밋은 새 커밋이 올라올 때까지 다시 시도하지 않습니다. 패키지 설치는 되돌리지 않습니다.
- CI가 실패했거나 실행 기록이 없는 커밋(`[skip ci]`)은 배포하지 않습니다. 고친 커밋을 올리면 그 커밋이 통과한 뒤 함께 반영됩니다.
- 확인: `journalctl -t water-deploy.sh`(반영·미룸·실패 기록만. `-u water-deploy`는 2분마다 남는 실행 기록까지 나옴), `water-deploy.sh check`(지금 할 일만 보여 주고 아무것도 바꾸지 않음). 잠시 멈추기는 `systemctl stop water-deploy.timer`, 다시 켜기는 `systemctl start water-deploy.timer`.
- 서버에서 레포 파일을 직접 고치지 않습니다. 고친 파일이 다음 커밋과 겹치면 배포가 멈춥니다(`git merge` 실패, 로그에 남음). 고칠 것은 GitHub에 올립니다.

## 파일과 서버 위치

| 파일 | 서버 위치 | 권한 |
|---|---|---|
| `systemd/water-monitor.service` | `/etc/systemd/system/` | 644 |
| `systemd/water-refresh.service`, `systemd/water-refresh.timer` | `/etc/systemd/system/` | 644 |
| `systemd/water-backup.service`, `systemd/water-backup.timer` | `/etc/systemd/system/` | 644 |
| `systemd/water-storage.service`, `systemd/water-storage.timer` | `/etc/systemd/system/` | 644 |
| `systemd/water-deploy.service`, `systemd/water-deploy.timer` | `/etc/systemd/system/` | 644 |
| `water-refresh.sh`, `water-backup.sh`, `water-storage.sh`, `water-deploy.sh` | `/usr/local/sbin/` | 755 |
| `water-cli` (운영 설정으로 관리 명령을 water 사용자로 실행) | `/usr/local/sbin/` | 700 |
| `nginx/water-monitor.conf` (HTTPS, 로그인 요청 제한, 8000번으로 전달) | `/etc/nginx/sites-available/water-monitor` | 644 |
| `water-monitor.env.example` | `/etc/water-monitor.env` (값을 채워서) | 600, root |
| `water-backup.env.example` | `/etc/water-backup.env` (값을 채워서) | 600, root |
| `water-deploy.env.example` | `/etc/water-deploy.env` (값을 채워서) | 600, root |

경로는 지금 서버 기준입니다: 앱 `/home/water/app`(water 사용자), 파이썬 `/home/water/miniforge3/envs/hanul`, 운영 자료 `/var/lib/water-monitor`, 백업 사본 `/var/backups/water-monitor`. 서버를 새로 만들면 같은 경로로 맞추거나 파일 안 경로를 바꿉니다.

## 깔거나 고칠 때 (root)

레포를 비공개로 바꿀 때만 토큰 파일을 root가 만듭니다: `install -m 600 -o root -g root /dev/null /etc/water-deploy.env` 뒤 `water-deploy.env.example`의 형식대로 `GITHUB_TOKEN='...'` 한 줄을 넣습니다. 비공개 레포에 이 파일이 없으면 자동 배포가 `GitHub에서 main을 받지 못함`으로 멈춥니다. 공개 레포인 지금은 없어도 됩니다.

root 설정 파일을 water 사용자가 쓸 수 있는 체크아웃에서 가져오므로, 설치 전에 그 파일이 GitHub `main`과 같은지 확인합니다(water 계정이 뚫린 뒤 고쳐 둔 스크립트가 root로 깔리는 일을 막음). 아래 두 확인 명령은 아무것도 출력하지 않아야 합니다. 출력이 있으면 설치하지 말고 무엇이 다른지 봅니다.

```sh
cd /home/water/app
sudo -u water git fetch --quiet origin main
sudo -u water git status --porcelain -- deploy/        # 서버에서 고친 파일이 없어야 한다
sudo -u water git diff --stat origin/main -- deploy/   # GitHub main과 같아야 한다
install -m 755 deploy/water-refresh.sh deploy/water-backup.sh deploy/water-storage.sh deploy/water-deploy.sh /usr/local/sbin/
install -m 700 deploy/water-cli /usr/local/sbin/
install -m 644 deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now water-monitor water-refresh.timer water-backup.timer water-storage.timer water-deploy.timer
systemctl list-timers 'water-*'          # 03:30 백업, 08:05 갱신, 보관(00:15·12:15·14:00·18:15 중 다음 시각), 2분마다 배포가 보이면 된다
```

Nginx(인증서를 받은 뒤):
```sh
install -m 644 deploy/nginx/water-monitor.conf /etc/nginx/sites-available/water-monitor
ln -sf /etc/nginx/sites-available/water-monitor /etc/nginx/sites-enabled/water-monitor
nginx -t && systemctl reload nginx
```
인증서 자동 갱신은 `/etc/crontab`의 한 줄입니다(IP 인증서는 약 6일짜리라 하루 두 번 확인).
```
0 0,12 * * * root /opt/certbot/bin/python -c 'import random; import time; time.sleep(random.random() * 3600)' && certbot renew -q
```

서버에서 이 파일들을 고쳤으면 여기에도 같은 내용으로 올려 둡니다.

## 백업에서 되돌리기 (root)

```sh
set -a; . /etc/water-backup.env; set +a
DAY=<되돌릴 날짜, 예: 2026-10-17>
mkdir -p /root/restore && cd /root/restore
aws --endpoint-url "$S3_ENDPOINT" s3 cp "s3://$BACKUP_BUCKET/daily/$DAY/" . --recursive
systemctl stop water-monitor water-refresh.timer water-backup.timer water-storage.timer water-storage.service water-deploy.timer
mv /var/lib/water-monitor /var/lib/water-monitor.broken-$(date +%F)   # 지우지 않고 옆으로 옮긴다
mkdir -p /var/lib/water-monitor
tar -xzf data.tar.gz -C /var/lib/water-monitor
rm -f /var/lib/water-monitor/.object-storage-index.json   # 다음 보관이 버킷과 처음부터 다시 대조해 맞춘다
[ -f snapshot.tar.gz ] && tar -xzf snapshot.tar.gz -C /var/lib/water-monitor
gunzip -c app.sqlite3.gz > /var/lib/water-monitor/app.sqlite3
chown -R water:water /var/lib/water-monitor
chmod 700 /var/lib/water-monitor
chmod 600 /var/lib/water-monitor/app.sqlite3
systemctl start water-monitor water-refresh.timer water-backup.timer water-deploy.timer
# 되돌린 자료를 확인한 뒤에 켠다(멈춘 사이 보관 시각이 지났으면 켜자마자 버킷을 되돌린 상태로 맞춘다)
systemctl start water-storage.timer
```
백업한 날 이후에 수집된 자료는 다음 자동 수집이 빠진 날짜를 찾아 다시 채웁니다.

## 함께 알아 둘 것

- 모델 학습 이력·계량기 매핑·역 위치 초기 자료(고객번호·역명·사용량이 든 CSV·JSON)는 저장소가 아니라 `/var/lib/water-monitor/history/`에 있습니다(`APP_HISTORY_DIR`로 바꿀 수 있음). 폴더 구조는 옛 `data/`와 같고(`history/processed`, `history/billing`, `history/app_seed`, `history/ml_dataset`, `history/daily`), 보관이 버킷 `history/`로 올리고 `restore`가 되살립니다. 2026-10-22에 저장소에서 자료를 빼면서 서버 체크아웃의 `data/{processed,billing,app_seed,ml_dataset,daily}`를 여기로 옮겼습니다.
- 03:30 백업은 날짜별로 되돌리는 용도입니다. 앱 안의 Object Storage 보관 기능(`back/storage`)은 수집 자료 보관과 새 서버 복원용이고, 08:05 갱신이 끝날 때와 00:15·12:15·14:00·18:15에 `water-storage.sh`가 돌립니다. 백업 사본은 데이터 폴더 밖에 만들어서 두 기능이 겹치지 않습니다.
- 웹 설정(`/etc/water-monitor.env`)에는 `OBJECT_STORAGE_` 줄을 넣지 않습니다. 이 파일은 08:05 `water-refresh`도 읽어서, 넣으면 웹과 갱신 안의 자동 훅이 켜집니다. 훅은 수집 워커 안에서 시간 제한 없이 돌아서 스토리지가 막히는 날 08:00 수집 마무리(재시도·실패 메일·완료 표시)와 모델 갱신이 몇 시간 밀립니다. 보관은 버킷 `hanwool-data`의 `prod/`를 쓰고(코드 기본값 `mulsaemi`는 팀원 PC 시험용), 키는 03:30 백업용 서브 계정 키를 이 작업에만 넘깁니다.
- 팀원 인수인계 문서(`docs/ncp-object-storage-handoff.md`)의 웹 설정에 넣고 켜기(5-1 3번, 5-4 1번), 기본 접두어 `mulsaemi`, root 셸에서 `python -m back.storage` 실행은 이 서버에서 하지 않습니다. 이 방식에서는 `audit_log`의 `storage_db_backup`이 생기지 않으니, 확인은 `journalctl -u water-storage`로 합니다.
- 운영 DB 백업은 `hanwool-data`에 올리지 않습니다(2026-10-22부터). 채팅의 읽기 키가 `prod/` 전체를 읽을 수 있어서, 거기 둔 DB 사본(비밀번호 해시·세션·전 사업소 고객번호·청구서)이 그 키로 내려받힙니다. DB 사본은 03:30 백업이 `hanwool-water-backup`에만 둡니다. 예전에 올라간 사본은 root에서 한 번 비웁니다: `set -a; . /etc/water-backup.env; set +a; aws --endpoint-url "$S3_ENDPOINT" s3 rm s3://hanwool-data/prod/backups/sqlite/ --recursive`.
- 보관 명령(`python -m back.storage …`)은 root로 직접 돌리지 않고 `water-storage.sh <명령>`으로 돌립니다(예: `status`, `plan`, `selftest`, `verify`). root로 돌리면 자료 폴더에 root 소유 잠금·색인 파일이 생겨 보관과 03:30 백업이 깨집니다.
- 새 서버를 버킷에서 되살릴 때는 웹과 타이머를 켜기 전에 `install -d -o water -g water -m 700 /var/lib/water-monitor` → `water-storage.sh restore /var/lib/water-monitor`로 수집·모델 파일을 받고(`failed`가 `[]`인지 확인), DB는 위 '백업에서 되돌리기'대로 03:30 백업의 `app.sqlite3.gz`에서 가져옵니다(`--db`는 이제 받을 것이 없음). 이미 있는 파일은 덮어쓰지 않으니 웹이 먼저 떠서 `app.sqlite3`가 생겼으면 웹을 멈추고 `app.sqlite3*`를 옆으로 옮긴 뒤 다시 합니다. 돌고 있는 서버에서는 쓰지 않고 `--overwrite`도 쓰지 않습니다. 접두어 `prod`가 들어 있어 팀원 PC 자료와 섞이지 않습니다. 이력 자료(`history/`)도 같이 받습니다.
- 운영 DB는 root로 열지 않습니다(DB 옆에 root 소유 파일이 생겨 앱이 망가질 수 있음). 확인할 때는 `sudo -u water sqlite3 -readonly /var/lib/water-monitor/app.sqlite3 "..."`.
- 코드 반영은 자동입니다(위 '자동 배포'). 자동 배포를 멈춰 두고 직접 반영할 때: `cd /home/water/app && sudo -u water git pull --ff-only` → 패키지가 바뀌었으면 `sudo -u water /home/water/miniforge3/envs/hanul/bin/python -m pip install -r requirements-dev.txt` → 화면이 바뀌었으면 `su - water -c "cd ~/app && npm ci && npm run build"` → `systemctl restart water-monitor`.
