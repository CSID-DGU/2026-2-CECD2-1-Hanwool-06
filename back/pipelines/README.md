# Private collection and model refresh

This directory contains the active pipelines. Retired collectors, ETL scripts and
saved experiments are catalogued in [`archive/README.md`](../../archive/README.md).
Archived files are not runtime inputs. The initial risk dataset is maintained at
`data/app_seed/risk/test_anomalies.csv`; new model results belong to `APP_DATA_DIR/model`.

All collection uses registered contracts from the application database. Archived and
inactive contracts are excluded. The API's incremental worker probes both daily readings
and bills for active Arisu contracts, including newly registered bill-only contracts.
The explicit range CLI uses `daily_enabled` for daily water/ridership collection.
Other providers remain visible in the catalog but are not sent to Arisu.

The web server owns the daily schedule (fixed at 08:00 Asia/Seoul), persisted jobs, and
manual updates. Meters that failed on a network error (timeout, connection, HTTP 429/5xx) or
were interrupted are retried twice, 30 minutes apart; a rejected login is never retried because
each retry logs in again. When the day's run settles (`collection_auto_settled` in `settings`),
active superadmins get one mail listing what still failed.
To pause collection, set `COLLECTION_WORKER_ENABLED=false` on the server
and restart it; queued jobs wait until it is enabled again. Registration/restoration queues a
connection check automatically. A job shares one authenticated Arisu session across
its meters. Empty results are not proof of an invalid customer number. Source records
must identify the requested customer before a connection is verified. Errors and empty
responses never delete previous values. The same customer cannot be registered twice.
For a connection test, update an existing meter or use a separate temporary database.

Incremental collection fills missing dates, rechecks the recent three months and recent
bill months, and probes the past twelve months for a new customer. Older empty water
months are retried weekly. New observations publish a snapshot; risk evaluation keeps
its prior date until a model refresh. The Arisu update does not call the ridership API
or train the model. Use the explicit CLI below for those operations.

The current Arisu login uses the site's issued CSRF token and `mbrId`/`pwd` form.
The account's registered customer list is checked for daily readings only. Public bills
use customer number and the exact registered name, independently of member registration.
A daily access failure does not prevent bill collection. Historical JSON readings and the latest unbilled
JSON readings are merged by observation date, through the last completed day.
A day the source itself leaves missing or invalid is excluded, reported once as a notice
rather than a failed collection (a failure when no reading was usable at all), and remembered. Identical readings repeated in different
billing periods are kept once; conflicting
observations and mismatched customer responses fail validation. Levy pages are
paginated, and invalid cached customer/date records are fetched again.

Configure `ARISU_USER_ID`, `ARISU_USER_PWD`, and `SEOUL_PSGR_KEY` in the server environment
or root `.env`. Environment values take priority. Legacy `I121_USER_ID/I121_USER_PWD`
are accepted only as a credential fallback. No credentials are needed for fixture tests.

When `OBJECT_STORAGE_ENABLED=true`, the end of each web collection job, `refresh` run and
monthly ridership import copies changed files under `APP_DATA_DIR` to NCP Object Storage, and the
API uploads one SQLite backup per day after the automatic run settles (`back/storage/README.md`).

The ridership API serves only the most recent week. Older dates come from the Seoul Open
Data monthly files (OA-12914, no key): `python -m back.pipelines.daily_ridership.monthly
2026-05 2026-08`. It writes the same per-date files and applies the station/line rule of
the historical `data/processed/ridership.csv`, so the series continues without a break.

```sh
python -m pip install -r back/api/requirements.txt -r back/pipelines/requirements.txt
python -m back.pipelines.refresh --start 2026-06-01 --end 2026-06-30 --water --ridership
python -m back.pipelines.refresh --start 2026-06-01 --end 2026-08-31 --bills
python -m back.pipelines.refresh --model
python -m back.pipelines.refresh --all
python -m unittest discover -s tests -p test_pipeline.py
```

LightGBM also needs an OpenMP runtime. On macOS install `libomp` or configure the
existing environment's library directory; Python dependencies alone do not supply it.

The default interval is the recent five completed KST dates. `--model` reads existing
local observations; collection only runs for explicitly selected sources. Missing
credentials fail the selected collection, without changing the last published snapshot.

Raw readings, successful partial results, per-date status files, datasets and model
results are under `APP_DATA_DIR` (default `data/runtime`). A missing/failed fetch never
writes zero usage or replaces previous observations with an empty file. Monthly water
queries are reused across the requested range. CSV upserts use customer number + date;
regular and supplementary bills retain separate keys. Retry the same range to fill
missing observations safely. A date's status distinguishes fresh completeness from
older rows retained after a failed request.

`refresh_report.json` records selected steps and failures. `snapshot/manifest.json`
points to one immutable `versions/<id>` containing `daily.json` and `risk.json`.
Publication changes this pointer atomically after both files validate. Private data
is never exported to `front/public` or committed to collection/snapshot Git branches.
The authenticated API reads the snapshot and applies user scope.
After publication, generated UUID snapshot directories older than 24 hours are removed,
except the newest three generations. The web worker and CLI serialize writes with a
private file lock. Queue ownership is also locked across API processes, with takeover
after an owning process exits. Interrupted work is reported and can be retried from
the web interface; queued jobs persist across restarts.

Historical `data/processed` observations are reused. The recovered ridership file
includes all 80 reviewed contracts. Old water CSV aliases are resolved through the
reviewed `data/billing/meter_match.csv`; new data carries its customer number directly.
The historical water file's `검침일` is the reading date: Arisu records a day's use on the next day, so it is moved
back one day to the usage day (`사용일`) that keys collected water files, ridership and the calendar.
Unknown aliases fail validation rather than being guessed. Calendar coverage is
regenerated for the available observations. Display labels come from the registry.

Model defaults run on CPU and use the current `back/ml/lightgbm` code. The final model
uses the validation model's best iteration. Residual center/scale and anomaly thresholds
are fixed from validation; later test observations cannot change earlier scores.
Bill baselines use only bills available before the observation. Historical exports lack
an exact issue date, so the first day after the ending billing month is the conservative
availability boundary; incomplete one-month buckets are excluded from this baseline.
Each `--model` run rebuilds that bill file (`APP_DATA_DIR/processed/water_bills.csv`) from the
historical export plus every regular bill collected since, without new bill requests.

A contract needs at least 90 training observations and 14 validation observations for
risk publication. New contracts still show observations; `manifest.withheld` records
why risk is unavailable. Existing train/validation date cutoffs are preserved. After
reviewing a newer evaluation period, pass `--train-end YYYY-MM-DD --valid-end YYYY-MM-DD`
to retrain with updated boundaries. With no earlier runtime split the defaults are 2026-04-30 and
2026-08-31. `--test-end YYYY-MM-DD` ends the evaluated period there;
later observations stay in the snapshot and are simply not scored. Model configuration and
result location can also be selected with `python -m back.ml.lightgbm.main --config path.yaml --out-dir path`.

A remote reading that stops moving is not used either. When a contract whose usual use (median
of the 28 observations before, without days already marked) is at least 3 t reports 0 t on three or
more observations in a row, those days and the first day the value moves again are removed from the
training, validation and scored rows. A run starts only on a 0 t day observed right after the
previous day, and missing days inside a run do not end it. Because history features are row based,
the days after a stall are compared with the level before it. The snapshot shows those days as
`자료 확인` risk rows with `reason` (`값 그대로` or `다시 움직인 첫날`), `since`, `days` and `usual`,
only inside the dates the published risk already covers.

The optional, manually dispatched Actions workflow uses a trusted self-hosted runner and persistent
`APP_DATA_DIR` outside its checkout. Initialize its database and contract registry
first and configure repository secrets and storage variables. Scheduling belongs to the
web application so setting `COLLECTION_WORKER_ENABLED=false` there also stops automatic Arisu collection. Source
code is checked out from `main`; observations and
snapshots remain in private persistent storage. There is no Git push or public artifact
upload. Concurrent refresh jobs share one Actions concurrency group and the collection
file lock. Do not point separate installations at the same database with different
data directories.

The web application's one-contract collector is `incremental.collect_meter`. It probes
daily readings only when daily collection is enabled, fills missing historical
months, and retries the recent three months. Contracts without observations start with the
latest twelve calendar months. Older empty months are retried after seven days; empty
results never prove that a customer number is invalid. The reviewed collection setting is preserved after collection. Public bill details refresh
recent charges, usage periods and separate water/groundwater readings. Missing source
fields do not erase stored details or user-entered addresses.
Failed bill windows are recorded for a subsequent retry even if older imported rows
already cover their months.

Application jobs can pass one authenticated `session` to reuse it for the entire batch;
the caller owns that session. Missing credentials, authentication failures, empty responses,
and partial success have separate results. All network checks are mocked in
`python -m unittest discover -s tests -p test_incremental.py`.

Web collection and `refresh` CLI writes share the private `.collection.lock` file lock.
Updated observations are published with existing risk results; model analysis still runs
separately. Snapshot cleanup runs only after successful publication and retains every
version from the last 24 hours plus at least the three latest versions.

## 자료 갱신 (운영 메모)

- **사용량·청구서(아리수)**: `.env`에 `ARISU_USER_ID`, `ARISU_USER_PWD`를 넣으면 화면의 **정보 업데이트**로 수집하고, 매일 08:00(한국시간)에 자동 수집합니다(서버가 켜져 있어야 함). 시간 초과·연결 같은 일시 오류로 실패하거나 서버 재시작으로 중단된 계량기는 30분 간격으로 두 번까지 다시 수집하고(로그인·설정·자료 오류는 계정 잠김을 막으려고 다시 시도하지 않음), 그 뒤에도 실패가 남으면 총괄 관리자에게 하루 한 번 메일을 보냅니다(메일 설정 필요). 음수·빈 검침은 그날만 제외하고 0으로 바꾸지 않습니다. 이런 날은 아리수 원자료에 값이 없는 것이라 수집 실패로 세지 않고 결과에 한 번 안내하며, 기억해 두었다가 다시 알리지 않습니다. 아리수 계정의 고객 목록에 두 번 연속 없는 고객번호는 일일 관제에서 자동으로 빼고 청구서만 수집합니다(계정에 등록한 뒤 계량기 관리에서 다시 켤 수 있음). 목록이 통째로 비어 오면 조회 실패로 보고 설정을 바꾸지 않습니다.
- **승하차**: 일별 API(`SEOUL_PSGR_KEY`, 최근 1주일치만 제공)와 서울 열린데이터광장 월별 파일(OA-12914, 키 불필요, 그 달이 지난 뒤 게시)로 채웁니다.
- **모델**: 웹 안의 자동 수집은 모델을 다시 돌리지 않습니다(운영 서버는 08:05 `water-refresh`가 돌립니다). 수집 뒤 아래 명령을 실행하면 수집된 마지막 날까지 위험도가 갱신됩니다. 모델을 돌릴 때마다 지금까지 수집된 청구서까지 합쳐 청구서 기준값을 다시 만듭니다.

```sh
python -m back.pipelines.refresh --start 2026-09-20 --end 2026-09-22 --water --ridership   # CLI 수집
python -m back.pipelines.daily_ridership.monthly 2026-05 2026-08                           # 승하차 월별 파일 보충
python -m back.pipelines.refresh --model                                                   # 저장된 자료로 모델 다시 실행
python -m back.pipelines.refresh --model --train-end 2026-04-30 --valid-end 2026-08-31     # 분할 경계를 새로 정할 때
```

결과와 오류는 `APP_DATA_DIR/refresh_report.json`에 남습니다. 모델 자료 구성에는 `APP_DATA_DIR/history/`의 이력 자료가 필요합니다([`../../data/README.md`](../../data/README.md)).
