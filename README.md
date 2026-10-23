# 서울교통공사 상수도 관리

서울교통공사 역사의 수도 사용량을 매일 모아 평소와 다른 사용을 찾아내는 웹 서비스입니다. 동국대학교 종합설계 한울 팀이 만들었습니다.

- 아리수 원격검침에서 역별 일일 사용량과 청구서를 매일 08:00에 자동으로 수집합니다.
- 서울 열린데이터광장의 승하차 자료를 함께 모아 사용량과 견줍니다.
- LightGBM 모델이 계량기별 사용량을 예측하고, 실측과의 차이를 정상·주의·경고로 나눕니다.
- 화면에서 노선도와 역별 추이, 청구 내역을 보고, 경고가 생기면 담당자에게 메일을 보냅니다.
- 채팅 도우미 물샘이에게 역과 날짜별 상태를 말로 물어볼 수 있습니다.

React/Vite 화면, FastAPI API, SQLite DB, LightGBM 모델로 이루어져 있고, 운영 자료는 NCP Object Storage에 보관합니다.

## 폴더

| 경로 | 내용 |
|---|---|
| `front/` | 웹 화면 |
| `back/api/` | API, 계정과 계량기 관리, 자동 수집 예약 |
| `back/pipelines/` | 아리수 사용량과 청구서 수집, 승하차 수집, 모델 갱신 |
| `back/ml/lightgbm/` | 예측과 이상탐지 모델 |
| `back/rag/` | 물샘이 채팅의 조회 도구와 문서 검색 |
| `back/storage/` | NCP Object Storage 보관과 복원 |
| `deploy/` | 운영 서버 설정(systemd, nginx)과 자동 배포 |
| `docs/` | 운영 담당자 문서 |

## 실행

Python 3.11 이상, Node.js 20.19 이상이 필요합니다. 저장소 루트에서 실행합니다.

```sh
conda env create -f environment.yml && conda activate hanul
npm install
cp .env.example .env    # ADMIN_EMAIL, ADMIN_PASSWORD를 채우면 첫 실행 때 총괄 관리자가 만들어집니다
npm run dev:api         # API 127.0.0.1:8000
npm run dev             # 화면 127.0.0.1:5173
```

한 프로세스로 띄우려면 `npm run build && npm run start`입니다. 사용량과 청구서를 받으려면 `.env`에 아리수 계정(`ARISU_USER_ID`, `ARISU_USER_PWD`)을 넣습니다.

## 테스트

```sh
python -m unittest discover -s tests
npm test
```

`main`에 올리면 GitHub Actions가 같은 검사와 화면 빌드를 돌리고, 운영 서버는 통과한 커밋만 받아 반영합니다.

## 문서

- 운영 서버 설정, 자동 배포, 백업: [`deploy/README.md`](deploy/README.md)
- 수집과 모델 갱신: [`back/pipelines/README.md`](back/pipelines/README.md), [`back/ml/README.md`](back/ml/README.md)
- Object Storage 보관과 복원: [`back/storage/README.md`](back/storage/README.md), [`docs/ncp-object-storage-handoff.md`](docs/ncp-object-storage-handoff.md)
- 물샘이 채팅: [`docs/chat-agent.md`](docs/chat-agent.md)
- 이력 자료의 위치와 구조: [`back/storage/README.md`](back/storage/README.md)
- 운영 설정과 관리 명령(DB 백업, 비밀번호 재설정): [`back/README.md`](back/README.md)
