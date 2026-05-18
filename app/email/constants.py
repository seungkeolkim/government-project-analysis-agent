"""Phase A-1 (task 00104) 메일 인프라 SystemSetting 키 + default 값 상수.

본 모듈은 SystemSetting 테이블에 저장되는 ``email.*`` 7개 키의 **이름** 과 **default
값** 을 한 곳에 모은다. 후속 subtask(``app/email/config.py``, sender, admin API)
가 본 모듈의 상수를 import 해 일관되게 사용한다.

설계 결정 (``docs/phase_a1_design_note.md`` §2-4 채택안 A):
    - SystemSetting 의 default 는 **별도 Alembic seed migration 으로 row 를 미리
      INSERT 하지 않는다.** 백업 도메인(00094) 의 패턴과 동일하게, row 가 없으면
      코드 레벨에서 본 모듈의 ``DEFAULT_*`` 상수로 fallback 한다.
    - row 가 실제로 생기는 시점은 관리자가 ``PUT /api/admin/email/settings`` 로
      자격증명을 처음 저장할 때 (subtask 00104-9 에서 구현).
    - 따라서 본 subtask(00104-2) 는 \"seed = 코드 상수 도입\" 으로 해석된다.

설계 결정 (``docs/phase_a1_design_note.md`` §2-5):
    - ``email.max_retry_count`` 는 SystemSetting.value(Text) 에 문자열 ``\"2\"`` 로
      저장하지만, 본 모듈의 ``DEFAULT_EMAIL_MAX_RETRY_COUNT`` 는 **int** 로 둔다.
      호출 측이 ``int(get_setting(...) or str(DEFAULT_EMAIL_MAX_RETRY_COUNT))`` 또는
      안전 캐스트 (``ValueError`` catch) 로 변환한다 — backup 의 ``_get_max_count_from_db``
      와 동일 방어 패턴.

비-결정 (range / format):
    - ``email.transport.type`` 의 허용 값은 현재 ``\"m365_oauth\"`` 단 1개.
      ``ALLOWED_EMAIL_TRANSPORT_TYPES`` 에 명시적으로 frozenset 으로 둬, factory
      (subtask 00104-6) 가 다른 값을 받으면 ``ValueError`` 를 던지도록 한다.
      향후 옵션 C (Basic Auth SMTP) 추가 시 본 frozenset 에 값을 더하고 factory
      분기를 한 줄 추가하면 된다 (Transport ABC 확장).
    - ``email.max_retry_count`` 의 UI/API 입력 범위는 0~5 (첨부 phase_a1_prompt.md
      \"재시도 횟수\" form spec). 본 상수 모듈은 단순 default 만 들고, 범위 검증은
      Pydantic schema (subtask 00104-9) 에서 수행한다.

비-범위 (이 모듈에서 하지 말 것):
    - get/set 헬퍼 함수 추가 금지. ``app.backup.service`` 의
      ``get_setting`` / ``set_setting`` 을 그대로 import 해 재사용한다 (디자인
      노트 §2-3).
    - SystemSetting row 의 실제 read/write 로직 도입 금지. 본 모듈은 \"이름 +
      default 값\" 만 들고, 호출 로직은 ``app/email/config.py``(00104-4) 가 만든다.
"""

from __future__ import annotations

# ──────────────────────────────────────────────────────────────
# SystemSetting key 이름 상수 (DB 의 ``system_settings.key`` 컬럼 값)
# ──────────────────────────────────────────────────────────────


# Transport 종류 선택 키. 현재 ``\"m365_oauth\"`` 단일 값만 허용.
SETTING_KEY_EMAIL_TRANSPORT_TYPE: str = "email.transport.type"

# M365 OAuth 자격증명 3종. IT 팀이 Azure AD app registration 셋업 완료 후 사용자
# 에게 전달하는 값들이며, 사용자가 관리자 페이지에서 입력해 SystemSetting 으로 저장.
SETTING_KEY_EMAIL_M365_TENANT_ID: str = "email.m365.tenant_id"
SETTING_KEY_EMAIL_M365_CLIENT_ID: str = "email.m365.client_id"
SETTING_KEY_EMAIL_M365_CLIENT_SECRET: str = "email.m365.client_secret"

# 발신 mailbox 주소. IT 가 SendAs 권한을 부여한 mailbox.
SETTING_KEY_EMAIL_M365_SENDER_ADDRESS: str = "email.m365.sender_address"

# From 헤더의 표시명 (수신자 메일 클라이언트가 보여주는 이름).
SETTING_KEY_EMAIL_FROM_DISPLAY_NAME: str = "email.from_display_name"

# 발송 실패 시 추가 재시도 횟수. 1차 시도는 별도 — 본 값이 2 이면 총 시도는 최대 3회.
SETTING_KEY_EMAIL_MAX_RETRY_COUNT: str = "email.max_retry_count"

# 메일 전송 기능 전체 활성화 스위치. 값 포맷: "true" / "false" (소문자 통일).
# row 가 없거나 빈 값이면 DEFAULT_EMAIL_SEND_ENABLED (=False) 로 fallback — 최초
# 기동 시 의도적으로 off 상태를 보장하기 위해 default 가 False 다.
SETTING_KEY_EMAIL_SEND_ENABLED: str = "email.send_enabled"

# 메일 본문에 넣는 공고 상세 페이지 URL 의 prefix (Phase A-2 Part 2 / task 00109).
# email.* 키는 아니지만 "메일 본문 조립 전용" 용도라 같은 모듈에 둔다 — 별도 모듈
# 신설은 over-engineering (docs/phase_a2_part2_design_note.md §7). 운영자가 외부
# 노출 URL(예: http://team-server.lan:8000) 로 바꿀 수 있도록 SystemSetting 으로
# 둔다. row 가 없으면 아래 DEFAULT 상수로 fallback — seed migration 없음.
SETTING_KEY_APP_PUBLIC_BASE_URL: str = "app.public_base_url"


# ──────────────────────────────────────────────────────────────
# Default 값 상수 (SystemSetting row 가 없을 때 fallback 으로 사용)
# ──────────────────────────────────────────────────────────────


# Transport 종류 default. 현재 단일 허용 값 ``\"m365_oauth\"``.
DEFAULT_EMAIL_TRANSPORT_TYPE: str = "m365_oauth"

# M365 자격증명 3종은 default 가 **빈 문자열**. 사용자가 관리자 페이지에서
# IT 가 알려준 값을 직접 입력해야 한다 — 빈 값으로는 발송이 불가능하며
# transport (00104-5) / sender (00104-8) 에서 명확한 에러 메시지로 안내한다.
DEFAULT_EMAIL_M365_TENANT_ID: str = ""
DEFAULT_EMAIL_M365_CLIENT_ID: str = ""
DEFAULT_EMAIL_M365_CLIENT_SECRET: str = ""

# 발신 mailbox default. IT 측에서 이 mailbox 에 SendAs 권한을 셋업하는 것이
# 외부 전제 (첨부 phase_a1_prompt.md '핵심 결정' 섹션). 사용자는 IT 셋업 후
# 본인 환경의 실제 mailbox 로 변경 가능.
DEFAULT_EMAIL_M365_SENDER_ADDRESS: str = "gov-agent-noreply@innodep.com"

# From 헤더 표시명 default. 한글 문구 그대로 첨부 spec 의 표 값.
DEFAULT_EMAIL_FROM_DISPLAY_NAME: str = "정부사업 모니터링 봇"

# 재시도 횟수 default. int 로 둔다 (저장 시 호출자가 str 화).
DEFAULT_EMAIL_MAX_RETRY_COUNT: int = 2

# 메일 전송 기능 활성화 default. 최초 기동 시 off(False) 가 의도된 동작이다.
# 저장 포맷은 "true" / "false" (소문자) — 읽을 때 case-insensitive 비교.
DEFAULT_EMAIL_SEND_ENABLED: bool = False

# 공고 상세 URL prefix default. SystemSetting row 가 없을 때 사용한다. 운영
# 환경에서는 관리자가 실제 외부 노출 URL 로 변경한다 (현재 admin UI 입력란은
# Part 2 범위 밖 — 필요 시 DB 직접 수정 또는 후속 task).
DEFAULT_APP_PUBLIC_BASE_URL: str = "http://localhost:8000"


# ──────────────────────────────────────────────────────────────
# 도메인 제약 상수
# ──────────────────────────────────────────────────────────────


# 현재 코드에서 유효한 transport 종류. factory (00104-6) 가 이 frozenset 으로
# 검증해, 다른 값이 SystemSetting 에 들어 있으면 즉시 ValueError 를 던진다.
# 향후 옵션 C (Basic Auth SMTP) 추가 시 이 frozenset 에 값을 더하고 factory 의
# 분기를 한 줄 추가하면 된다.
ALLOWED_EMAIL_TRANSPORT_TYPES: frozenset[str] = frozenset({"m365_oauth"})


# ``EmailSendRun.related_kind`` 컬럼에 채워지는 A-1 범위 값. A-2 (forward),
# A-3 (daily_report) 가 추가되면 본 상수 옆에 또 다른 RELATED_KIND_* 가
# 정의된다.
RELATED_KIND_TEST_SEND: str = "test_send"

# ``EmailSendRun.related_kind`` 컬럼에 채워지는 A-2 Part 2 (공고 포워딩) 값.
# forwarding service (00109-3) 가 수신자별 ``send_with_retry`` 호출 시
# ``related_kind=RELATED_KIND_FORWARD`` / ``related_id=EmailForwardLog.id`` 로
# 넘겨, 발송 이력 expand 조회 시 EmailForwardLog 와 EmailSendRun 을 잇는다.
RELATED_KIND_FORWARD: str = "forward"


# ``EmailSendRun.transport_type`` 컬럼에 채워지는 A-1 범위 값. 현재
# ``DEFAULT_EMAIL_TRANSPORT_TYPE`` 와 동일하지만, 의미상 \"SystemSetting 의 default
# 값\" 과 \"이력 row 의 transport_type 표기\" 는 별개 책임이므로 별도 상수로 분리해
# 의미를 명확히 한다 (둘이 동시에 바뀌어야 하는 일이 생기면 그 시점에 정렬).
TRANSPORT_TYPE_M365_OAUTH: str = "m365_oauth"


# ──────────────────────────────────────────────────────────────
# 키 집합 (호출자 편의)
# ──────────────────────────────────────────────────────────────


# 7개 SystemSetting 키 전체 — 관리자 페이지의 \"메일 설정\" 섹션이 한 번에 GET/PUT
# 하는 단위. 키 추가/제거 시 본 tuple 만 갱신하면 후속 코드가 자동 반영된다.
EMAIL_SETTING_KEYS: tuple[str, ...] = (
    SETTING_KEY_EMAIL_TRANSPORT_TYPE,
    SETTING_KEY_EMAIL_M365_TENANT_ID,
    SETTING_KEY_EMAIL_M365_CLIENT_ID,
    SETTING_KEY_EMAIL_M365_CLIENT_SECRET,
    SETTING_KEY_EMAIL_M365_SENDER_ADDRESS,
    SETTING_KEY_EMAIL_FROM_DISPLAY_NAME,
    SETTING_KEY_EMAIL_MAX_RETRY_COUNT,
    SETTING_KEY_EMAIL_SEND_ENABLED,
)


# 키 → default 값 매핑 (호출자가 한 번에 fallback 처리할 때 사용). value 가
# 다양한 타입(str/int) 을 섞고 있어 명시적으로 ``object`` 타입으로 둔다 — 호출
# 측에서 키별로 type narrowing 한다.
EMAIL_SETTING_DEFAULTS: dict[str, object] = {
    SETTING_KEY_EMAIL_TRANSPORT_TYPE: DEFAULT_EMAIL_TRANSPORT_TYPE,
    SETTING_KEY_EMAIL_M365_TENANT_ID: DEFAULT_EMAIL_M365_TENANT_ID,
    SETTING_KEY_EMAIL_M365_CLIENT_ID: DEFAULT_EMAIL_M365_CLIENT_ID,
    SETTING_KEY_EMAIL_M365_CLIENT_SECRET: DEFAULT_EMAIL_M365_CLIENT_SECRET,
    SETTING_KEY_EMAIL_M365_SENDER_ADDRESS: DEFAULT_EMAIL_M365_SENDER_ADDRESS,
    SETTING_KEY_EMAIL_FROM_DISPLAY_NAME: DEFAULT_EMAIL_FROM_DISPLAY_NAME,
    SETTING_KEY_EMAIL_MAX_RETRY_COUNT: DEFAULT_EMAIL_MAX_RETRY_COUNT,
    SETTING_KEY_EMAIL_SEND_ENABLED: DEFAULT_EMAIL_SEND_ENABLED,
}


__all__ = [
    "ALLOWED_EMAIL_TRANSPORT_TYPES",
    "DEFAULT_APP_PUBLIC_BASE_URL",
    "DEFAULT_EMAIL_FROM_DISPLAY_NAME",
    "DEFAULT_EMAIL_M365_CLIENT_ID",
    "DEFAULT_EMAIL_M365_CLIENT_SECRET",
    "DEFAULT_EMAIL_M365_SENDER_ADDRESS",
    "DEFAULT_EMAIL_M365_TENANT_ID",
    "DEFAULT_EMAIL_MAX_RETRY_COUNT",
    "DEFAULT_EMAIL_SEND_ENABLED",
    "DEFAULT_EMAIL_TRANSPORT_TYPE",
    "EMAIL_SETTING_DEFAULTS",
    "EMAIL_SETTING_KEYS",
    "RELATED_KIND_FORWARD",
    "RELATED_KIND_TEST_SEND",
    "SETTING_KEY_APP_PUBLIC_BASE_URL",
    "SETTING_KEY_EMAIL_FROM_DISPLAY_NAME",
    "SETTING_KEY_EMAIL_M365_CLIENT_ID",
    "SETTING_KEY_EMAIL_M365_CLIENT_SECRET",
    "SETTING_KEY_EMAIL_M365_SENDER_ADDRESS",
    "SETTING_KEY_EMAIL_M365_TENANT_ID",
    "SETTING_KEY_EMAIL_MAX_RETRY_COUNT",
    "SETTING_KEY_EMAIL_SEND_ENABLED",
    "SETTING_KEY_EMAIL_TRANSPORT_TYPE",
    "TRANSPORT_TYPE_M365_OAUTH",
]
