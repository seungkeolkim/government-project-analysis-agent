# 메일 발송 Transport 옵션 비교 (Phase A-1, task 00104)

본 문서는 정부과제 모니터링 봇이 사용 가능한 SMTP 발송 경로 3 가지를 비교한다.
현재 단독 구현되어 있는 경로는 **옵션 B (M365 OAuth XOAUTH2)** 이며, 다른 두
옵션은 향후 확장 가능성을 두고 명세만 기록해 둔다.

`app/email/transport/base.py` 의 `EmailTransport` ABC 가 이 3 옵션을 같은
인터페이스 (`send(message: EmailMessage) -> None`) 로 추상화하므로, 옵션 추가
시 코드 변경 범위는 다음 4 곳으로 제한된다:

1. `app/email/transport/<new_option>.py` — 새 구현체 1 개 추가.
2. `app/email/transport/factory.py` — `elif transport_type == ...:` 분기 1 줄 추가.
3. `app/email/constants.py` — `ALLOWED_EMAIL_TRANSPORT_TYPES` frozenset 에 값 추가.
4. (옵션) 새 SystemSetting 키가 필요하면 `app/email/config.py` 에 dataclass +
   loader 한 쌍 추가.

다른 layer (sender / API / frontend) 는 인터페이스만 알면 되므로 수정 불필요.

----

## 비교표

| 항목 | 옵션 A: port 25 + IP 기반 | 옵션 B: M365 OAuth XOAUTH2 | 옵션 C: Basic Auth SMTP |
|---|---|---|---|
| **현재 상태** | **미사용** (구 A-0 폐기) | **단독 구현** (Phase A-1) | 미구현, 향후 추가 가능 |
| **host** | 회사 M365 inbound connector (예: `<tenant>.mail.protection.outlook.com`) | `smtp.office365.com` | `smtp.office365.com` |
| **port** | 25 | 587 | 587 |
| **인증 방식** | 인증 없음 — 송신 IP 화이트리스트 기반 | OAuth 2.0 client credentials + SMTP AUTH XOAUTH2 | SMTP AUTH PLAIN/LOGIN |
| **자격증명** | 송신 서버 고정 IP (Exchange Online connector 등록) | Azure AD app: `tenant_id` / `client_id` / `client_secret` (3 개) | mailbox 계정의 username + password (또는 app password) |
| **TLS** | optional / opportunistic | STARTTLS (587) | STARTTLS (587) |
| **From 제약** | connector 허용 도메인만 발송 가능. 발신자 mailbox 사전 등록 불필요. | Azure AD app 에 `SMTP.SendAsApp` Application 권한 + 발신 mailbox 의 SendAs 권한 부여 필요. From 헤더의 mailbox 주소가 권한 부여된 값과 정확히 일치해야 함. | 인증한 mailbox 자기 자신만 발신 가능. SendAs 권한이 있어도 본 흐름에서는 별도 처리 필요. |
| **수신 도메인** | 내부 / 외부 모두 가능 (connector 설정에 따라) | 내부 / 외부 모두 가능 (M365 정책 한도 내 — 분당 30개, 일일 10,000개) | 내부 / 외부 모두 가능, 동일 한도 |
| **발송 위치 제약** | 송신 서버가 화이트리스트된 IP 에서만 가능 — 사무실 / VPN / 클라우드 인스턴스에 IP 고정 또는 NAT 게이트웨이 필요. | 위치 제약 없음 — 인터넷 접근만 되면 어디서든 발송 가능. M365 Conditional Access 정책에서 클라이언트 인증을 차단하지 않는 환경 전제. | M365 정책상 Basic Auth 가 차단된 테넌트에서는 불가. 회사 정책에 따라 사용 가능성이 달라진다. |
| **외부 의존 라이브러리** | `smtplib` (표준) | `msal` + `smtplib` | `smtplib` (표준) |
| **장점** | 자격증명 관리 부담 없음 — IP만 등록하면 끝. | 표준 OAuth 흐름. Conditional Access 정책 통과 용이. 비밀번호 만료 없음. | 가장 단순. 외부 라이브러리 없음. mailbox 단위 권한 분리 쉬움. |
| **단점** | spoofing 위험 + 송신 IP 변경 시 connector 재등록 필요. **회사 M365 정책상 IP 단독 허용은 다른 계정에도 영향을 미쳐 IT 협조 불가** (구 A-0 폐기 사유). | Azure AD app registration + SendAs 권한 부여라는 일회성 IT 셋업 필요. `client_secret` 평문 DB 저장 (현재 정책 — A-1 범위 밖). | M365 Conditional Access / Modern Auth 강제 환경에서 차단됨. 보안 측면에서도 비권장. |

## 자격증명 / 설정 SystemSetting 키 (옵션 B 전용, 현재 활성)

| key | type | default seed | 비고 |
|---|---|---|---|
| `email.transport.type` | string | `m365_oauth` | 현재 이 값만 유효 (ALLOWED_EMAIL_TRANSPORT_TYPES) |
| `email.m365.tenant_id` | string | (빈 값) | IT 가 제공하는 Directory (tenant) ID |
| `email.m365.client_id` | string | (빈 값) | IT 가 제공하는 Application (client) ID |
| `email.m365.client_secret` | string (secret) | (빈 값) | Client secret VALUE. 평문 저장 — DB-level 암호화는 향후 별도 작업 |
| `email.m365.sender_address` | string | `gov-agent-noreply@innodep.com` | IT 가 SendAs 권한 부여한 mailbox |
| `email.from_display_name` | string | `정부사업 모니터링 봇` | From 헤더 표시명 |
| `email.max_retry_count` | int (text 로 저장) | `2` | 발송 실패 시 추가 재시도 횟수 (1 차 시도 별도) |

7 개 키 모두 SystemSetting (`system_settings` 테이블) 의 key-value 로 저장된다.
환경변수는 일체 사용하지 않으며, 관리자가 「시스템 관리」 → 「메일 발송」 →
「메일 설정」 폼에서 즉시 변경 가능하다 (변경 후 다음 발송부터 반영).

자세한 입력 절차는 [README.USER.md 의 「메일 발송 설정」 섹션](../README.USER.md#메일-발송-설정) 참조.

## 옵션별 코드 위치

| 옵션 | 구현 파일 | factory 분기 |
|---|---|---|
| A | (미구현) | (없음 — 폐기) |
| B | `app/email/transport/m365_oauth.py` | `factory.py` 의 `transport_type == \"m365_oauth\"` |
| C | (미구현, 예: `app/email/transport/basic_auth_smtp.py`) | (미작성 — 추가 시 `elif transport_type == \"basic_auth_smtp\"`) |

## 옵션 A 폐기 결정 (구 A-0 prompt)

이전 라운드의 A-0 plan 은 옵션 A 를 단독 구현 경로로 두었으나, 다음 사유로
폐기되었다 (Phase A-1 핵심 결정):

- 회사 M365 정책상 IP 단독 허용은 spoofing 위험이 크다.
- 단일 IP 화이트리스트가 다른 계정의 발송 동작에도 영향을 미쳐 IT 협조 불가.
- 결과적으로 옵션 B 가 단독 구현 경로로 채택되었으며, A-2 (공고 포워딩) /
  A-3 (daily report) 등 후속 phase 도 같은 transport 위에 올린다.

본 결정은 phase_a1_prompt.md 의 \"전체 프로젝트 로드맵 컨텍스트\" 섹션에 기록되어
있으며, 향후 누군가 옵션 A 의 재도입을 검토할 때 이 문서의 위 사유를 참고한다.

## 향후 옵션 C 추가 시 점검 리스트

1. IT 측에서 Basic Auth (SMTP AUTH PLAIN/LOGIN) 가 차단되어 있지 않은지 확인.
   M365 의 \"Authenticated SMTP\" 정책을 mailbox 단위로 허용해야 한다.
2. 자격증명 (username + password 또는 app password) 을 SystemSetting 에 추가.
   기존 `email.m365.*` 와 충돌하지 않도록 별도 prefix (예: `email.basic.*`) 사용 권장.
3. `app/email/transport/basic_auth_smtp.py` 신규 구현체 작성 — `smtplib.SMTP.login()`
   호출 후 `send_message()`.
4. `app/email/transport/factory.py` 에 `elif transport_type == \"basic_auth_smtp\"`
   분기 추가. `app/email/constants.py` 의 `ALLOWED_EMAIL_TRANSPORT_TYPES` 에도
   문자열 추가.
5. `app/web/templates/admin/email.html` 의 Transport 종류 select 에서 `disabled`
   속성 제거 + 새 option 추가. `app/web/routes/admin_email.py` 의 PUT schema 에
   `transport_type` 필드 추가하고 검증 로직 연결.
