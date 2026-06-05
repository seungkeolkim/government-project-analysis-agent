"""스케줄러 도메인 단위 테스트 패키지.

task 00155 에서 APScheduler 를 제거하고 OS cron + crontab 으로 전환한 뒤로, 본
디렉터리는 cron 작업 CLI(``app.scheduler.run_job``), 스케줄 설정 저장소
(``app.scheduler.schedule_store``), crontab 텍스트 생성기/설치기
(``app.scheduler.crontab_generator`` / ``crontab_installer``) 회귀를 가드한다.
디렉터리가 패키지가 되도록 마커로서 빈 ``__init__.py`` 를 둔다.
"""
