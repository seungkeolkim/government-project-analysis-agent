"""government-project-analysis-agent 의 최상위 애플리케이션 패키지.

하위 모듈:
    - config:        환경변수 기반 설정 로더
    - logging_setup: 로거 초기화
    - db:            SQLAlchemy 모델/세션 (후속 subtask에서 채움)
    - scraper:       Playwright 기반 IRIS 스크래퍼 (후속 subtask에서 채움)
    - services:      스크래핑→DB 적재 등 오케스트레이션 (후속 subtask에서 채움)
    - web:           FastAPI 로컬 열람 웹 (후속 subtask에서 채움)

이 파일은 부트스트랩 subtask에서는 패키지 선언 목적만 수행한다.
"""

__all__: list[str] = []
