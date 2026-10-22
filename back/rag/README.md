# 채팅의 원격 자료와 검색 문서

`ChatTools`는 SQLite에서 현재 계량기 소유 사업소·활성·삭제·일일 수집 여부만 읽는다. 사용량·승하차·LightGBM 판정은 **NCP Object Storage**의 `<prefix>/snapshot/manifest.json`과 그 manifest가 지정한 `versions/<id>/daily.json`, `risk.json`에서 읽는다. 로컬 스냅샷이나 저장소 seed로 대체하지 않는다. 웹 화면의 로컬 자료와 원격 게시 시점이 다르면 값이 다를 수 있으므로 도구 응답에 `snapshot_version`, `snapshot_generated_at`을 포함한다.

환경변수는 `CHAT_STORAGE_BUCKET`(필수), `CHAT_STORAGE_PREFIX`(기본 `prod`), `CHAT_STORAGE_ENDPOINT`(기본 `https://kr.object.ncloudstorage.com`), `CHAT_STORAGE_REGION`(기본 `kr-standard`), `CHAT_STORAGE_ACCESS_KEY`, `CHAT_STORAGE_SECRET_KEY`다. 키가 없으면 기존 `config.OBJECT_STORAGE_ACCESS_KEY/SECRET_KEY`를 사용하며 이 설정은 `NCP_ACCESS_KEY/SECRET_KEY` 별칭도 지원한다. `OBJECT_STORAGE_ENABLED`를 켜지 않는다. 채팅은 객체 GET만 수행한다. 배포 계정에도 해당 prefix의 GET 권한만 부여할 수 있다.

생성자는 접속하지 않으며 실제 조회 때 접속한다. manifest는 `published: true`와 안전한 버전 경로가 필요하다. 하루 자료 파일은 각각 최대 64MiB, manifest는 1MiB다. 객체의 SHA-256 metadata가 있으면 검증한다. 게시 버전이 바뀌어 이전 객체가 정리된 경우에만 새 manifest로 한 번 재시도한다. 최신 manifest 확인에 실패하면 캐시를 대신 반환하지 않는다. 프로세스 메모리에 최대 두 버전, 60초 동안 캐시하며 endpoint·bucket·prefix·인증키 해시·버전·manifest 해시로 분리한다. 네트워크 예산을 요청의 남은 시간으로 제한하고 스트림을 읽을 때마다 마감시간을 확인한다.

## 실제 매뉴얼과 검증 사례 등록

현재 다운로드한 버킷 사본에는 `rag/`가 없다. 없는 자료를 검색 결과로 꾸미지 않으며 매뉴얼·사례가 미등록임을 반환한다. 배포 운영자가 검토한 문서를 아래 형식으로 업로드해야 검색할 수 있다. 코드나 테스트가 임의의 검증 사례를 운영 버킷에 만들지는 않는다.

`<prefix>/rag/index.json`의 예시(형식 설명용):

```json
{
  "documents": [
    {"key": "manuals/leak-check.md", "title": "누수 점검 매뉴얼", "kind": "manual", "office_ids": null},
    {"key": "manuals/east-check.md", "title": "동부 점검 절차", "kind": "manual", "office_ids": ["동부"]},
    {"key": "cases/verified.jsonl", "title": "현장 확인 완료 사례", "kind": "case", "office_ids": ["동부"]}
  ]
}
```

`office_ids`에는 표시 이름을 추측해서 쓰지 말고 운영 DB의 실제 사업소 ID를 넣는다. `null`은 전체 사용자가 열람할 수 있는 매뉴얼에만 쓴다. 사례 색인에는 허용 사업소 목록이 필요하다. 사업소가 맞지 않는 문서는 다운로드 전에 제외한다. 선택적으로 각 문서 항목에 `sha256`을 지정할 수 있다. index는 최대 24개 문서/128KiB, 문서 하나는 최대 512KiB다.

매뉴얼은 UTF-8 Markdown이며 문단별로 검색한다. 사례 파일은 UTF-8 JSONL이며 한 줄이 다음 필드를 갖는다. 아래는 실제 사건이 아닌 **입력 형식**이다.

```json
{"title":"검토한 사례 제목","office_id":"실제 사업소 ID","meter_id":"실제 계량기 ID","date":"2026-10-20","verified":true,"source":"검토한 현장 기록 식별자","text":"현장에서 확인한 증상, 원인, 실제 조치와 결과"}
```

`verified`가 불리언 `true`이고 `source`가 있으며 날짜·사업소·활성 계량기 권한이 모두 맞는 행만 검색한다. 미검증 사례를 true로 올리지 않는다. 파일당 최대 2,000줄이다. 검색은 한글·영문 단어와 두 글자 부분 일치를 사용하는 가벼운 어휘 검색이며 임계값을 넘는 상위 3개 구절만 반환한다. 문서 내용은 외부 자료이므로 모델에 `untrusted_document: true`를 전달한다. 내용의 고객번호는 제거하고 모델이 문서 내 지시를 따르지 않도록 대화 계층에서 제한한다.

## 서버 인터페이스와 조회 범위

```python
from back.rag.tools import ChatTools, TOOL_SCHEMAS
kit = ChatTools(office_ids=authorized_office_ids, context=saved_server_context, deadline=deadline)
result = kit.execute("get_station_status", {"station": "동부역", "line": None, "meter_ref": None, "date": None})
```

- `TOOL_SCHEMAS`: Responses API strict function schema. 모든 필드를 전달하고 선택 값은 `null`로 지정한다. 서버가 이름·필드·타입·날짜·범위를 다시 검증한다.
- `resolve_station(query, line)`, `get_station_status(meter_ref, station, line, date)`, `get_station_history(meter_ref, station, line, start_date, end_date)`, `find_anomalies(meter_ref, station, line, start_date, end_date, severity)`, `search_manuals(query)`, `search_similar_cases(query)`.
- 상태·이력·이상 조회의 `station`, `line`, `meter_ref`는 모두 `null`을 허용한다. 함께 지정하면 교집합으로 조회하며, 모두 `null`이면 현재 권한의 활성 계량기 전체를 조회한다. 이전 대화에서 선택한 역이 전체 조회 범위를 몰래 좁히지 않는다. `resolve_station`의 후보 표시는 최대 12개이며, 전체 자료 집계에는 이 제한을 적용하지 않는다.
- `get_station_history`와 `find_anomalies`의 시작일·종료일은 `null`도 허용한다. 종료일이 `null`이면 선택 범위의 원격 최신 자료일, 시작일이 `null`이면 종료일 6일 전이다. 따라서 날짜를 모두 생략하면 최신 원격 자료일까지 7일을 조회한다. `get_station_status`의 `date=null`은 선택 범위의 최신 자료일 하루를 조회한다. 명시한 날짜는 유지하며 원격 기준일이 없으면 현재 시각이나 로컬 자료로 대체하지 않는다.
- 사용일 기준 ISO 날짜의 유효성과 시작일·종료일 순서만 검사하며 기간 상한은 없다. 저장된 날짜만 순회하여 전체 사용량·상태·순위를 집계한다. 이력의 일별 상세는 최대 4개 계량기에서 총 120행, 상태 조회의 계량기 상세와 이상 조회의 최근 상세는 각각 최대 11건이다. 응답 크기가 크면 이력 상세 행을 더 줄이지만 전체 집계는 유지한다. 상태·이상 조회와 개별 계량기 이력의 생략 여부는 `truncated`, 이력 전체의 생략 여부는 `summary.meters_truncated`·`summary.rows_truncated`로 알린다. 상태·이력의 전체 집계는 `summary`에, 이상 조회 집계는 응답 최상위에 담긴다. 모두 실제 검색 집계 출처를 갖는다.
- `coverage`에는 요청 기간, 일일 수집 대상 계량기 수, 기대 계량기·사용일 수, 실제 사용량·분석 자료 수, 누락 수, 각각의 첫날·마지막 날과 날짜 수, 요청 기간과 상관없이 저장된 마지막 날(`stored_last_date`)이 담긴다. 같은 날짜에 두 계량기의 판정이 있으면 판정 건수는 2건이고 날짜 수는 1일이다. `severity_counts`와 `severity_day_counts`로 이를 구분한다. 분석 자료가 일부만 있는 기간의 일치 0건을 전체 기간에 이상이 없었다는 뜻으로 설명하면 안 된다.
- 같은 계량기·사용일이 중복되면 게시 파일의 마지막 기록 한 건을 사용한다. 합계와 판정 횟수, 상세 출처에도 같은 기록을 사용한다.
- 오류 검침은 `자료 확인`, 정상 관측이 있으나 판정이 없으면 `미분석`, 관측도 없으면 `자료 없음`, 청구 전용이면 `일일 수집 대상 아님`이다. 실제 사용량 0은 유효하며 결측을 0으로 채우지 않는다.
- 이력 통계는 유효 관측만 사용한다. 일별 상세 `rows`에는 실제 저장된 날짜만 담고, `period_days`와 누락 수는 요청 기간 전체를 기준으로 계산한다. 전일·전주 같은 요일 비교와 최근 7일/직전 7일 합계·평균·관측일수를 계산한다. 두 주가 모두 7일 관측을 갖춘 경우에만 주간 총량 증감을 계산한다. 비교를 위한 14일 자료는 같은 원격 버전에서 읽는다.
- 음수 변화율을 “71.4% 감소”처럼 설명할 수 있도록 부호 있는 값과 함께 서버 계산 절댓값(`*_magnitude`)·증감 방향을 근거에 넣는다. 결측은 그대로 `null`이며 조회 기간 일수는 `period_days`로 명시한다.
- `kit.context`는 서버 저장용으로 실제 ID 매핑을 포함한다. 모델에는 `kit.model_context`와 도구 응답만 보낸다. 모델은 불투명 `meter_ref`만 사용한다.
- `kit.sources`는 서버가 만든 `{id,label,kind,meter_id,date,href,summary}` 배열이다. 실제 계량기·날짜가 있으면 `#/detail?meter=...&date=...`를 만들고, 공통 매뉴얼·전체 조회 집계에는 링크가 없을 수 있다. `kit.evidence[source_id]`는 모델에 전달한 수치·문구의 검증 근거다. 문서가 모델에 만든 링크를 출처로 채택하지 않는다.

로컬 검증은 `python -m unittest discover -s tests -p 'test_chat_storage.py'`, `test_chat_tools.py`, `test_rag.py`로 실행한다. 가짜 S3를 사용하므로 실제 NCP 접속·업로드는 하지 않는다.
