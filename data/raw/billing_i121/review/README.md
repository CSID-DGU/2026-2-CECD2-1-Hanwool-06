mkey와 역명 매핑의 기준이 되는 검토용 Excel 파일을 두는 폴더입니다.

과거 자동 후처리 도구는 [`archive/back/scripts/billing_etl/postprocess_bills.py`](../../../../archive/back/scripts/billing_etl/postprocess_bills.py)에 보관했습니다. 현재 추가 검토표 반영은 `python -m back.api.import_data --review /path/review.xlsx`로 파일을 명시해 실행합니다.
