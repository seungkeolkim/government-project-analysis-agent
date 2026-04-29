"""Playwright 기반 대시보드 E2E 테스트 패키지 (task 00043-4).

본 패키지는 단위 테스트 (``tests/dashboard/*``) 가 커버할 수 없는 영역 —
실제 브라우저 (chromium headless) 가 임베드 JSON 을 파싱하고 Chart.js 로
캔버스를 그리는 흐름, CSS computed style 분기, 폼 제출 후 페이지 재렌더 — 을
검증한다.

서버 기동 정책 (사용자 원문 task 00043 §3):
    \"서비스 포트 8000 은 이미 쓰고 있으니 변경이 필요할 수 있어.\"
    → 본 E2E 는 8001 포트로 격리된 uvicorn 서브프로세스를 띄운다 (8000 미점유).

DB 격리:
    각 E2E 모듈이 ``tmp_path`` 의 SQLite 파일 1개를 만들고 ``DB_URL`` env var
    로 우회시킨다. 운영 DB ``data/db/app.sqlite3`` 와는 완전히 분리된다.
"""
