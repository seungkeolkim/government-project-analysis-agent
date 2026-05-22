"""공유 렌더링 라이브러리 패키지 (task 00136).

대시보드(``app.web``)와 데일리 리포트 메일(``app.email``)이 함께 소비하는
표시용 view-model 과 HTML 조각 렌더러를 모아 두는 중립 네임스페이스다.
``app.web`` / ``app.email`` 어느 한쪽에도 속하지 않으므로 두 레이어 모두
순환 import 없이 이 패키지를 import 할 수 있다.
"""
