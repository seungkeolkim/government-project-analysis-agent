"""``app.email`` 패키지 단위 테스트 (Phase A-1 / task 00104-10).

외부 의존성 (msal / smtplib / 실제 SMTP 서버) 는 monkeypatch 로 차단해 외부
네트워크 호출 없이 통과해야 한다.
"""
