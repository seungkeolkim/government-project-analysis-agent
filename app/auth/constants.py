"""인증 관련 상수.

사용자 원문의 "보안" 섹션에 명시된 결정사항을 코드 상수로 고정한다.
각 상수의 근거는 바로 옆 주석에 남겨, 향후 값을 바꿀 때 "왜 이 값인가"를
직접 읽을 수 있도록 한다.

변경 금지 원칙:
    여기의 상수를 바꾸면 전역 보안 정책이 바뀐다. 수정 전에 사용자 원문과
    ``docs/auth_ui_design.md`` §11(보안 세부) 를 다시 읽는다.
"""

from __future__ import annotations

import re
from typing import Final

# ──────────────────────────────────────────────────────────────
# 쿠키
# ──────────────────────────────────────────────────────────────

# 쿠키 이름. 프로젝트 식별자(gpa = government-project-analysis) + session.
# 다른 애플리케이션의 쿠키와 이름 충돌을 피하고자 prefix 를 붙인다.
SESSION_COOKIE_NAME: Final[str] = "gpa_session"

# 쿠키 속성 기본값. route 층에서 set_cookie(...) 호출 시 이 값을 그대로 쓴다.
# - HttpOnly=True: JS 에서 document.cookie 로 접근 불가 → XSS 시 세션 탈취 차단.
# - SameSite="lax": 외부 사이트의 POST 를 자동 차단 (CSRF 기본 방어).
# - Secure=False: 로컬 전제 (HTTP). 추후 HTTPS 종단을 도입하면 True 로 바꾼다.
COOKIE_HTTP_ONLY: Final[bool] = True
COOKIE_SAMESITE: Final[str] = "lax"
COOKIE_SECURE: Final[bool] = False
COOKIE_PATH: Final[str] = "/"

# ──────────────────────────────────────────────────────────────
# 세션 수명
# ──────────────────────────────────────────────────────────────

# 세션 기본 유효기간. 사용자 원문 "기본 유효기간 30일 (상수 + 근거 주석)".
# 근거:
#   팀 공용 로컬 환경이라 구성원이 매일 재로그인하는 번거로움을 줄이는 것이
#   우선이다. 로컬 전제로 중요 권한이 세션에 걸려 있지 않으며 (관리자 기능은
#   Phase 2+ 에서 추가 게이트), Phase 2 스케줄러 도입 후 보다 짧은 수명을
#   검토할 수 있다.
SESSION_LIFETIME_DAYS: Final[int] = 30

# ──────────────────────────────────────────────────────────────
# 세션 토큰
# ──────────────────────────────────────────────────────────────

# secrets.token_urlsafe(n) 의 n(바이트). 32바이트 = 256비트 엔트로피.
# URL-safe base64 인코딩 결과는 약 43자로 DB 컬럼 String(64) 내에 여유 있게 담긴다.
SESSION_TOKEN_BYTES: Final[int] = 32

# ──────────────────────────────────────────────────────────────
# 비밀번호 / 아이디 정책
# ──────────────────────────────────────────────────────────────

# bcrypt rounds. passlib 기본과 동일한 12.
# 로컬 전제 + 사용자 규모가 작아 login 지연(≈100~150ms) 보다 해시 강도가 우선.
# 값 변경 시 기존 해시는 그대로 호환된다 (passlib 이 해시 문자열에서 파라미터
# 를 읽는다).
BCRYPT_ROUNDS: Final[int] = 12

# username 허용 패턴. 소문자 ASCII + 숫자 + 언더스코어, 3~64자.
# 팀 내 호환성 위해 대소문자 혼용 금지 — 입력을 lower() 로 정규화한 뒤 매칭.
USERNAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9_]{3,64}$")

# 비밀번호 최소/최대 길이. 최소 8자, 최대 128자.
# 최대값은 bcrypt 의 72바이트 제약을 훨씬 넘지 않도록 두는 범용 상한 — UTF-8
# 에서 한글/이모지 조합 시 72바이트 넘어가 암호 뒷부분이 잘리는 사고를 UX 로
# 방지한다.
PASSWORD_MIN_LENGTH: Final[int] = 8
PASSWORD_MAX_LENGTH: Final[int] = 128

# email 간이 검증 패턴. 엄격한 RFC 준수 대신 "@ 하나와 점 하나 이상" 수준으로
# 타이핑 실수만 걸러낸다. 이메일 검증은 사용자 원문 "이메일 검증 X" 에 따라
# Phase 1b 범위 밖이다.
EMAIL_LIGHT_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


__all__ = [
    "SESSION_COOKIE_NAME",
    "COOKIE_HTTP_ONLY",
    "COOKIE_SAMESITE",
    "COOKIE_SECURE",
    "COOKIE_PATH",
    "SESSION_LIFETIME_DAYS",
    "SESSION_TOKEN_BYTES",
    "BCRYPT_ROUNDS",
    "USERNAME_PATTERN",
    "PASSWORD_MIN_LENGTH",
    "PASSWORD_MAX_LENGTH",
    "EMAIL_LIGHT_PATTERN",
]
