# Phase 1b — 인증 + 읽음/안읽음 UI 부착 설계 노트

> **작성 범위**: Task 00021 (Phase 1b) — Phase 1a 로 깔린 `users` / `user_sessions` /
> `announcement_user_states` 테이블 위에 자유 회원가입·세션 쿠키 인증과
> 읽음/안읽음 UI 를 얹는다. **비로그인 열람(기존 URL 그대로)을 깨뜨리지 않는
> 것이 최우선 불변식**이다.
>
> 본 문서는 후속 subtask(00021-2 ~ 00021-7)가 인용할 수 있도록 절(§)
> 번호 체계를 유지한다. 구현 본문은 포함하지 않으며, 인터페이스 시그니처
> 수준만 담는다.

---

## §1. 스코프와 전제

### §1.1 이 task 에서 다루는 것
- `app/auth/` 신설 — 서비스(세션 발급/검증/삭제), 라우트, 의존성 함수, 상수.
- `app/db/models.py` 에 `UserSession` ORM 을 추가 (DB 테이블은 이미 존재).
- `app/db/repository.py` 에 인증·읽음표시용 헬퍼 함수 추가.
- `app/web/main.py` 의 기존 `index_page` / `detail_page` 에 "로그인 시에만"
  동작하는 읽음 표시 로직을 비침습적으로 합류.
- `app/web/templates/base.html` 상단 네비에 로그인 분기 섹션 추가.
- `app/web/templates/login.html` / `register.html` 신설.
- `app/web/templates/list.html` / `detail.html` 에 bold/normal class 분기와
  자동 읽음 처리 연동.
- `app/web/static/css/style.css` 에 `.site-nav`, `.announcement-title-link--unread`,
  `.auth-form`, `.flash-message` 등 추가.
- `scripts/create_admin.py` 신설 — 첫 admin 계정 생성 CLI.
- `tests/auth/` 신설 — 서비스·라우트·실사용자 리셋 흐름 단위/통합 테스트.
- 문서 갱신 — `README.md` / `README.USER.md`.

### §1.2 이 task 에서 다루지 않는 것 (범위 밖)
- 관리자 페이지·관리자 기능 UI (Phase 2).
- 즐겨찾기 폴더 UI / `favorite_entries` (Phase 3b).
- 관련성 판정 UI / bulk mark (Phase 3a).
- 이메일 검증 / 비밀번호 찾기 / 소셜 로그인.
- CSRF 토큰 발급/검증 (사용자 원문에서 로컬 전제 하 skip 명시 —
  same-origin 체크로만 방어한다. §10 참조).
- 세션 만료 정리 배치(자동 cleanup job) — 만료된 세션은 조회 시점에
  로그인 실패로 처리하고, 적극적 삭제는 Phase 2 의 스케줄러에 맡긴다.

### §1.3 절대 건드리지 않는 것
- 기존 `/` 와 `/announcements/{id}` 경로/응답 코드 — 비로그인에서도 404·400
  외 에러가 나서는 안 된다.
- `/attachments/{attachment_id}/download` — 로그인 요구 금지 (사용자 원문
  "비로그인 열람 유지 핵심").
- `Announcement` / `Attachment` / `User` / `AnnouncementUserState` ORM 정의
  및 baseline·phase1a migration 파일.

---

## §2. 현재 구조 요약 (탐사 결과)

### §2.1 라우트
`app/web/main.py` (540라인 단일 파일). 라우트는 클로저 방식으로 `create_app()`
내부에 선언됨.

| 메서드 | 경로 | 함수 | 렌더/응답 |
|---|---|---|---|
| GET | `/` | `index_page` | `list.html` |
| GET | `/announcements/{id}` | `detail_page` | `detail.html` |
| GET | `/announcements` | `list_announcements_api` | JSON |
| GET | `/attachments/{id}/download` | `attachment_download` | FileResponse |

DB 세션 의존성은 `get_session()` (요청 단위 `SessionLocal`). 커밋/롤백은
라우트가 명시적으로 하지 않는다 — 현재 라우트는 **전부 read-only** 이므로
session.close 만 수행된다. **읽음 UPSERT 를 추가하면 라우트가 최초로 쓰기
경로를 갖게 된다** (§6 참조).

### §2.2 템플릿 상속
- `base.html` — `<header class="site-header">` 에 사이트 타이틀만 있고
  네비게이션이 없다. `content` 블록 하나를 자식이 채운다.
- `list.html` — `{% extends "base.html" %}` 후 `content` 블록에서 필터 폼 +
  테이블을 렌더. 행 제목은 `.announcement-title-link` 클래스.
- `detail.html` — `{% extends "base.html" %}` 후 메타 테이블 +
  `viewers/{source}.html` include + 첨부 테이블. 브레드크럼은 `.breadcrumb`.
- `viewers/iris.html`, `viewers/ntis.html`, `viewers/default.html` — 상세 본문
  렌더러. 이번 task 에서 수정 불필요.

### §2.3 CSS
`static/css/style.css` 단일 파일, 외부 CDN 없음. 주요 클래스:
- 레이아웃: `.container`, `.site-header`, `.site-title`, `.site-main`, `.site-footer`
- 목록 행 제목: `.announcement-title-link` (font-weight: 500 기본)
- 상세: `.breadcrumb`, `.detail-title`, `.meta-table`
- 폼 공통: `.filter-input`, `.filter-submit` (재사용 가능)

시스템 폰트 스택과 `#1f2937` 본문 색을 쓰므로 인증 UI 도 동일 팔레트를 쓴다.

---

## §3. 모듈 배치 (신규/수정 파일 지도)

다음 파일 목록은 후속 subtask 의 "건드릴 파일" 목록이다.

### §3.1 신규 생성
```
app/auth/__init__.py               # 공개 심볼 재-export (아래 §4 참고)
app/auth/constants.py              # 상수: 쿠키 이름, 세션 수명, 해시 라운드 등
app/auth/service.py                # 비밀번호 해시/검증, 세션 발급/검증/삭제
app/auth/dependencies.py           # FastAPI Depends 용 current_user 2버전
app/auth/routes.py                 # APIRouter: /auth/register, /login, /logout, /me
                                   #           GET /login, GET /register (HTML)

app/web/templates/login.html       # 로그인 폼 (base.html extends)
app/web/templates/register.html    # 회원가입 폼 (base.html extends)

scripts/create_admin.py            # 첫 admin 계정 생성 CLI (getpass)

tests/auth/__init__.py
tests/auth/test_service.py         # 해시·세션 발급/만료 단위
tests/auth/test_routes.py          # register/login/logout/me HTTP 레벨
tests/auth/test_read_flow.py       # 실사용자 리셋 회귀 방지 통합 테스트
```

### §3.2 수정
```
app/db/models.py                   # UserSession ORM 모델 추가 (§4.1)
app/db/repository.py               # §5 헬퍼 함수 추가 (기존 함수 수정 금지)
app/web/main.py                    # auth router mount + index/detail 에 읽음 주입
app/web/templates/base.html        # 상단 네비 블록 추가 (§7.1)
app/web/templates/list.html        # 제목 링크에 is_read 기반 class 분기 (§8)
app/web/templates/detail.html      # 변경 없음(서버에서 UPSERT 로 처리)
app/web/static/css/style.css       # 네비, auth-form, unread 스타일 추가 (§9)

pyproject.toml                     # passlib[bcrypt] 의존성 추가 (§4.2)
README.md                          # "로컬 전용 — 인증 없음" 문구 → 로그인 설명
README.USER.md                     # 첫 관리자 계정 생성법 섹션
PROJECT_NOTES.md                   # MemoryUpdater 가 finalize 에서 갱신 (수동 수정 X)
```

---

## §4. 인증 코어 (app/auth/)

### §4.1 `UserSession` ORM (`app/db/models.py`)

DB 테이블은 Phase 1a migration 에서 이미 만들어졌으므로 **DDL 변경 없음**.
Phase 1b 는 ORM 선언만 추가한다. `users.user_sessions` 역관계도 함께 추가.

```python
class UserSession(Base):
    """로그인 세션. session_id 는 서버 발급 랜덤 문자열."""

    __tablename__ = "user_sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", name="fk_user_sessions_user_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow,
    )

    user: Mapped[User] = relationship("User", back_populates="sessions")
```

`User` 쪽 역관계:

```python
class User(Base):
    sessions: Mapped[list[UserSession]] = relationship(
        "UserSession", back_populates="user",
        cascade="all, delete-orphan", lazy="selectin",
    )
```

컬럼명/인덱스명/FK 이름은 `alembic/versions/20260422_1500_b2c5e8f1a934_phase1a_new_tables.py`
의 DDL 과 **정확히 일치**해야 한다 (Alembic autogenerate diff 가 비도록).

### §4.2 상수 (`app/auth/constants.py`)

```python
SESSION_COOKIE_NAME: Final[str] = "gpa_session"
# 30일 — 팀 공용 로컬 환경이라 자주 로그인 반복하지 않도록 충분히 길게.
# 사용자 원문: "기본 유효기간 30일 (상수 + 근거 주석)".
SESSION_LIFETIME_DAYS: Final[int] = 30

# secrets.token_urlsafe(32) → 약 43글자. DB 컬럼 String(64) 여유 내.
SESSION_TOKEN_BYTES: Final[int] = 32

# passlib bcrypt 기본 라운드. 로컬 전제 — 과도하게 높이지 않는다.
BCRYPT_ROUNDS: Final[int] = 12

USERNAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9_]{3,64}$")
PASSWORD_MIN_LENGTH: Final[int] = 8
```

의존성 추가 필요:
```toml
# pyproject.toml [project.dependencies]
"passlib[bcrypt]>=1.7,<2.0",
```
`passlib` 는 `bcrypt==4.x` 와 호환 이슈가 있을 수 있음 — `bcrypt>=4,<5` 를
함께 고정. 설치는 **Coder 가 아니라 WFC/Setup 에이전트가 수행한다**
(coder 는 패키지 설치 금지).

### §4.3 서비스 (`app/auth/service.py`)

모든 함수는 호출자가 전달한 `Session` 을 그대로 사용 (repository 와 동일
규약). 커밋 경계는 라우트 또는 `session_scope()` 가 결정한다.

```python
def hash_password(plain: str) -> str:
    """bcrypt 해시 문자열을 반환한다."""

def verify_password(plain: str, hashed: str) -> bool:
    """bcrypt 검증. 해시 포맷이 망가지면 False."""

def validate_username(value: str) -> str:
    """USERNAME_PATTERN 검증. 성공 시 lowercase 정규화 값 반환, 실패 시 ValueError."""

def validate_password(value: str) -> None:
    """길이·금지문자 검증. 실패 시 ValueError."""

def create_user(
    session: Session,
    *,
    username: str,
    password: str,
    email: str | None = None,
    is_admin: bool = False,
) -> User:
    """User row 생성. username 중복이면 DuplicateUsernameError.
    호출자가 commit. scripts/create_admin.py 도 이 함수를 공유한다."""

def authenticate(
    session: Session, *, username: str, password: str,
) -> User | None:
    """username + password 검증. 성공 시 User, 실패 시 None."""

def issue_session(
    session: Session,
    user: User,
    *,
    now: datetime | None = None,
) -> UserSession:
    """UserSession row 생성 + session_id 반환. now 기본 _utcnow().
    expires_at = now + SESSION_LIFETIME_DAYS."""

def resolve_session(
    session: Session,
    session_id: str,
    *,
    now: datetime | None = None,
) -> tuple[UserSession, User] | None:
    """쿠키 값으로 세션 + 사용자 조회. 만료(expires_at <= now)면 None.
    만료된 row 는 이 함수에서 건드리지 않는다 — 재로그인 흐름에서 교체된다."""

def revoke_session(session: Session, session_id: str) -> None:
    """해당 세션 row 삭제 (logout). 없어도 no-op."""


class DuplicateUsernameError(ValueError):
    """username UNIQUE 충돌."""
```

에러 처리: `create_user` 는 DB 레벨 UniqueConstraint 충돌 시 IntegrityError 를
잡아 `DuplicateUsernameError` 로 변환한다. 이렇게 하면 라우트가 서비스 타입
에러만 catch 하면 된다.

### §4.4 의존성 (`app/auth/dependencies.py`)

두 버전을 제공한다 (사용자 원문 "허용/필수 두 버전"):

```python
def current_user_optional(
    request: Request,
    session: Session = Depends(get_session),
) -> User | None:
    """쿠키가 없거나 세션이 만료면 None. 비로그인 경로용."""

def current_user_required(
    user: User | None = Depends(current_user_optional),
) -> User:
    """비로그인이면 401/302 로 거부. 이번 task 에서 실제로 쓰는 라우트는
    'GET /auth/me' 와 'POST /auth/logout' 에 한정된다(관리자 게이트는 Phase 2)."""
```

구현 메모:
- `current_user_optional` 은 **토큰을 DB 에서 SELECT 만** 한다. 세션 자체의
  `updated_at`/sliding window 갱신은 하지 않는다 (단순화).
- 만료 세션을 만난 경우 응답에서 쿠키를 지우는 로직은 **라우트가 필요할 때
  명시적으로** 한다 (의존성은 순수 read-only 여야 여러 라우트에서 공유하기
  쉽다). 로그인 페이지 진입 시 만료 쿠키를 자연스럽게 덮어쓰므로 실무상
  무해.

### §4.5 `app/auth/__init__.py` 재-export
라우트에서 `from app.auth import current_user_optional, AuthService` 식으로
쓸 수 있도록 공개 심볼만 노출.

---

## §5. Repository 헬퍼 추가 (§3.1 에서 수정)

`app/db/repository.py` 는 기존 함수에 영향이 없도록 **뒤에 append** 한다.

### §5.1 인증용
이 subtask 에서 별도 repository 함수를 쓰지 않고 서비스 레이어에서 직접
ORM 을 다뤄도 충분하다 — 이유: 질의가 전부 단순 PK lookup 이며 repository
계층의 공통 필터(`is_current`, 정렬 등)와 겹치지 않는다. **서비스가
repository 관례를 따라 `flush` 까지만 수행하고 `commit` 은 호출자에게
맡긴다**.

### §5.2 읽음 표시용 헬퍼
```python
def get_read_state_map(
    session: Session, *, user_id: int, announcement_ids: Iterable[int],
) -> dict[int, bool]:
    """announcement_id → is_read 맵을 한 번의 쿼리로 반환.

    N+1 방지용. 목록 페이지에서 페이지당 최대 page_size (기본 20,
    max 100) 건을 IN 절로 조회한다. 매칭 없는 id 는 맵에 키 자체가
    없거나 False 로 취급한다 (템플릿에서 dict.get(id, False))."""

def mark_announcement_read(
    session: Session, *, user_id: int, announcement_id: int,
    now: datetime | None = None,
) -> AnnouncementUserState:
    """UPSERT: (announcement_id, user_id) row 를 is_read=True, read_at=now
    로 설정. 없는 공고면 FK 에러가 나지 않도록 호출자가 announcement 존재를
    미리 검증한다(detail_page 는 404 분기 이후 호출)."""
```

구현 힌트:
- `get_read_state_map` 은 이미 있는 `AnnouncementUserState` SELECT WHERE
  `user_id = :uid AND announcement_id IN (:ids)` 한 방 쿼리.
- `mark_announcement_read` 는 sqlite 이식성 위해 `SELECT -> if missing INSERT
  else UPDATE` 단순 패턴으로 충분하다 (동일 트랜잭션 내 경합은 현실적으로
  없음, 있다면 UNIQUE 제약이 두 번째 삽입을 거절).

### §5.3 리셋 로직 재활용 확인
Phase 1a 의 `_reset_user_state_on_content_change` 는 `AnnouncementUserState`
를 `announcement_id` 만으로 매칭해 전 user 의 row 를 리셋한다.
**실제 User 가 생긴 이후에도 동일한 쿼리로 동작**하므로 수정 불필요.
§11 통합 테스트에서 이것이 재확인 포인트.

---

## §6. Auth 라우트 (`app/auth/routes.py`)

APIRouter 하나에 모아 `create_app()` 에서 `fastapi_app.include_router(...)`
로 mount.

### §6.1 시그니처
```python
router = APIRouter(prefix="", tags=["auth"])

@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request, user: User | None = Depends(current_user_optional)) -> HTMLResponse:
    """이미 로그인 상태면 '/' 로 redirect."""

@router.post("/auth/register")
def register_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    email: str | None = Form(default=None),
    session: Session = Depends(get_session),
) -> Response:
    """검증 → create_user → issue_session → 쿠키 Set-Cookie + redirect '/'."""

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, user: User | None = Depends(current_user_optional)) -> HTMLResponse:
    """이미 로그인 상태면 '/' 로 redirect."""

@router.post("/auth/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    session: Session = Depends(get_session),
) -> Response:
    """authenticate → issue_session → 쿠키 + redirect. 실패 시
    login.html 을 에러 메시지와 함께 재렌더."""

@router.post("/auth/logout")
def logout_submit(
    request: Request,
    session: Session = Depends(get_session),
) -> Response:
    """쿠키 읽어 revoke_session → 쿠키 삭제 + redirect '/'."""

@router.get("/auth/me")
def me(user: User | None = Depends(current_user_optional)) -> dict[str, Any]:
    """비로그인이면 {'user': null}, 로그인이면
    {'user': {'id': ..., 'username': ..., 'is_admin': ...}}."""
```

### §6.2 응답 / 리다이렉트 규칙
- POST 성공 → `RedirectResponse('/', status_code=303)` (POST-redirect-GET).
- POST 실패 → 동일 템플릿을 `flash: str | None` 컨텍스트와 함께 재렌더
  (세션 저장 없음, 쿼리 파라미터로 넘기지 않음 — 단순 유지).
- GET `/login`, `/register` 는 이미 로그인되어 있으면 `RedirectResponse('/')`.

### §6.3 쿠키 설정 (§10 보안 세부와 함께 1회 정의)
```python
response.set_cookie(
    key=SESSION_COOKIE_NAME,
    value=session_id,
    max_age=SESSION_LIFETIME_DAYS * 86400,
    httponly=True,
    samesite="lax",
    secure=False,   # 로컬 전제. HTTPS 종단 전까지는 False.
    path="/",
)
```
로그아웃 시:
```python
response.delete_cookie(SESSION_COOKIE_NAME, path="/")
```

### §6.4 POST same-origin 체크
CSRF 토큰은 쓰지 않는다 (로컬 전제). 대신 **POST 요청만** `Origin` 또는
`Referer` 헤더가 현재 host 와 일치하는지 가벼운 확인 (`app.auth.dependencies`
내 `ensure_same_origin(request)` 헬퍼). 실패 시 400 반환.

---

## §7. 템플릿 — 상단 네비와 인증 UI

### §7.1 `base.html` 개편
`<header class="site-header">` 안쪽을 다음 구조로 확장한다 (기존 타이틀/서브타이틀
유지, 네비 영역만 추가):

```jinja
<div class="container site-header__inner">
    <div class="site-header__brand">
        <h1 class="site-title"><a href="/">사업공고 로컬 열람</a></h1>
        <p class="site-subtitle">수집된 사업공고를 소스별로 조회한다.</p>
    </div>
    <nav class="site-nav" aria-label="사용자">
        {% if current_user %}
            <span class="site-nav__user">{{ current_user.username }}</span>
            <form class="site-nav__logout" method="post" action="/auth/logout">
                <button type="submit" class="site-nav__link site-nav__link--button">로그아웃</button>
            </form>
        {% else %}
            <a class="site-nav__link" href="/login">로그인</a>
            <a class="site-nav__link" href="/register">회원가입</a>
        {% endif %}
    </nav>
</div>
```

주입 규칙: `create_app()` 에서 `templates.env.globals["current_user"]` 를
쓰지 않는다 (요청마다 바뀌므로 global 부적절). 대신 **모든 HTML 라우트가
`templates.TemplateResponse(..., {..., "current_user": user})` 로 컨텍스트에
넣는다**. 템플릿에서 `current_user is not defined` 도 처리할 수 있도록
`{% if current_user %}` 방식을 사용 (None 과 undefined 모두 falsy).

### §7.2 `login.html`
```jinja
{% extends "base.html" %}
{% block page_title %}로그인{% endblock %}
{% block content %}
<section class="auth-form-wrap">
    <h2>로그인</h2>
    {% if flash %}<p class="flash-message flash-message--error">{{ flash }}</p>{% endif %}
    <form class="auth-form" method="post" action="/auth/login">
        <label>아이디 <input name="username" required autocomplete="username"></label>
        <label>비밀번호 <input type="password" name="password" required
            autocomplete="current-password"></label>
        <button type="submit" class="filter-submit">로그인</button>
    </form>
    <p class="auth-form__switch"><a href="/register">회원가입</a></p>
</section>
{% endblock %}
```

### §7.3 `register.html`
login.html 과 동일 뼈대. email 은 optional 입력 (`<input type="email" name="email">`).
약관/안내 문구는 **쓰지 않는다** (로컬 전제).

### §7.4 에러·성공 메시지
- 성공: redirect 만 하고 상단 flash 는 쓰지 않는다 (단순화).
- 실패: `flash` 문자열을 템플릿 컨텍스트로 넣어 `.flash-message` 로 표시.

---

## §8. 목록 페이지 — 읽음/안읽음 분기

### §8.1 백엔드 주입
`index_page` 안에서:

```python
user: User | None = Depends(current_user_optional)
...
if user is None:
    read_map: dict[int, bool] = {}
else:
    announcement_ids = [ann.id for ann, _size in ann_with_sizes]
    read_map = get_read_state_map(session, user_id=user.id,
                                  announcement_ids=announcement_ids)
```

그룹 모드(`group_mode=True`) 에서도 `gr.representative.id` 기준으로 동일
맵을 구한다. 확장 섹션 내 개별 공고(announcement) 링크도 같은 맵을 공유할
수 있다 (이번 task 에서는 상단 대표 row 만 분기해도 충분하다 — 범위 밖).

### §8.2 템플릿 분기 (`list.html`)
제목 링크 한 곳에 클래스 하나만 더한다:

```jinja
<a
    class="announcement-title-link
           {% if read_map.get(ann.id, False) %}announcement-title-link--read
           {% else %}announcement-title-link--unread{% endif %}"
    href="/announcements/{{ ann.id }}">
    {{ ann.title }}
</a>
```

비로그인 시 `read_map` 이 빈 dict 이므로 모든 링크가 "unread" 클래스를
갖는다. **비로그인 시 스타일 차이가 시각적으로 드러나면 안 되므로**,
§9 CSS 에서 "unread" 는 `font-weight: 700` 으로 강조하되, 상위 래퍼
`body.is-anonymous` 가 있을 때는 일반 weight 로 되돌린다:

```jinja
<body class="{% if not current_user %}is-anonymous{% endif %}">
```

(→ base.html 의 `<body>` 태그에 클래스 추가.)

### §8.3 N+1 방지 검증 포인트
- `get_read_state_map` 은 page_size 이하의 id 집합에 대해 **단일 쿼리**.
- `list_announcements` 와 독립된 쿼리이지만 동일 세션·동일 트랜잭션.
- Reviewer 가 로그 측면에서 쿼리 수(최대 2 + 기존 카운트) 로 확인한다.

---

## §9. 상세 페이지 — 자동 읽음 UPSERT

### §9.1 플로우
```
GET /announcements/{id}
 ├─ get_announcement_by_id → 없으면 404 (기존 분기 유지)
 ├─ user = current_user_optional(...)
 ├─ if user:  mark_announcement_read(session, user_id=user.id,
 │                                   announcement_id=announcement.id)
 │            session.commit()   # ← 라우트가 최초로 커밋을 수행하는 지점
 ├─ get_attachments_by_announcement  (기존 그대로)
 └─ render detail.html (current_user 컨텍스트 포함)
```

### §9.2 커밋 타이밍
`get_session()` 의존성은 현재 커밋하지 않고 close 만 한다. 읽음 UPSERT
이후 명시적으로 `session.commit()` 을 호출한다 — **읽음 데이터 손실이
사용자에게 혼란을 주지 않도록 세션 종료 전에 확정**한다. 실패 시
`session.rollback()` 후 200 응답은 유지 (페이지 자체는 계속 보여야 하므로).
로그는 WARNING 으로 남긴다.

### §9.3 IRIS/NTIS 분리 동작
`AnnouncementUserState` 는 `announcement_id` 단위이므로 사용자 원문
"IRIS 건 읽어도 NTIS 건은 유지" 가 **자동으로 충족**된다 (동일 canonical
그룹 내 다른 row 는 다른 id). 별도 처리 불필요.

### §9.4 detail.html
서버에서 UPSERT 하므로 템플릿 변경 없음. 단 `base.html` 의 nav 분기를
위해 `current_user` 는 컨텍스트에 반드시 포함한다.

---

## §10. CSS 추가 (`style.css` append)

기존 스타일과 충돌하지 않도록 파일 **하단에 새 섹션으로 append**.
(팔레트는 기존과 동일 — #1d4ed8 / #1f2937 / #6b7280.)

```css
/* ---------- 상단 네비 (Phase 1b) ---------- */
.site-header__inner { display: flex; align-items: flex-end;
    justify-content: space-between; gap: 16px; }
.site-header__brand { flex: 1; }
.site-nav { display: flex; align-items: center; gap: 12px; font-size: 14px; }
.site-nav__user { color: #374151; font-weight: 600; }
.site-nav__link { color: #1d4ed8; }
.site-nav__link--button { background: none; border: none; padding: 0;
    cursor: pointer; font-size: inherit; color: #1d4ed8; }
.site-nav__logout { display: inline; margin: 0; }

/* ---------- 인증 폼 ---------- */
.auth-form-wrap { max-width: 360px; margin: 40px auto; background: #ffffff;
    padding: 24px; border: 1px solid #e5e7eb; border-radius: 6px; }
.auth-form { display: flex; flex-direction: column; gap: 12px; }
.auth-form label { display: flex; flex-direction: column; gap: 4px;
    font-size: 14px; color: #374151; }
.auth-form input { padding: 8px 10px; border: 1px solid #d1d5db;
    border-radius: 4px; font-size: 14px; }
.auth-form__switch { margin-top: 12px; font-size: 13px; text-align: center; }
.flash-message { padding: 8px 12px; border-radius: 4px; font-size: 13px; }
.flash-message--error { background: #fee2e2; color: #991b1b;
    border: 1px solid #fecaca; }

/* ---------- 읽음/안읽음 (Phase 1b) ---------- */
/* 로그인 사용자 기준: 안 읽은 공고는 굵게, 읽은 공고는 기본. */
.announcement-title-link--unread { font-weight: 700; color: #111827; }
.announcement-title-link--read { font-weight: 400; color: #4b5563; }
/* 비로그인(body.is-anonymous) 시에는 두 클래스 모두 기본 스타일로 리셋. */
body.is-anonymous .announcement-title-link--unread,
body.is-anonymous .announcement-title-link--read
    { font-weight: 500; color: #1f2937; }
```

---

## §11. 보안 세부 (사용자 원문 "보안" 섹션 매핑)

| 항목 | 결정 | 근거 |
|---|---|---|
| 쿠키 HttpOnly | `true` | XSS 에서 세션 탈취 차단. |
| 쿠키 SameSite | `lax` | 외부 사이트의 GET 진입은 허용, POST 는 차단 — 로컬 전제에서도 CSRF 기본 방어. |
| 쿠키 Secure | `false` | HTTPS 미사용 로컬. Phase 2+ 에서 리버스 프록시 HTTPS 종단 시 env 로 전환 예정. |
| 세션 ID | `secrets.token_urlsafe(32)` | 256-bit 엔트로피. URL-safe 문자만. |
| DB 저장 | 평문 | 로컬 전제 — 쿠키 자체와 탈취 위험이 동일 (DB 파일 접근 = 쿠키 탈취 가능). |
| 만료 | `expires_at <= utcnow()` 면 로그인 실패 | `resolve_session` 이 일관되게 체크. |
| 기본 수명 | 30일 | `SESSION_LIFETIME_DAYS` 상수 + 근거 주석 (팀 공용 편의). |
| CSRF 토큰 | 미발급 | 로컬 전제. POST 는 §11.5 의 same-origin 체크로 최소 방어. |
| 비밀번호 해싱 | bcrypt (`passlib`) | 사용자 원문 명시. rounds=12. |
| 비밀번호 최소 길이 | 8 | 로컬 전제 기본선 — admin 은 더 길게 권장 (문서에 명시). |

### §11.5 Same-Origin 체크
```python
def ensure_same_origin(request: Request) -> None:
    """POST 라우트 진입 시 Origin/Referer 가 request.url.netloc 과 일치하는지 확인.
    외부 사이트가 만든 폼 submit 차단용 최소선."""
```
`POST /auth/register`, `/auth/login`, `/auth/logout` 에서 `Depends(ensure_same_origin)`.

---

## §12. `scripts/create_admin.py` 설계

```python
"""첫 관리자 계정 생성 CLI.

usage:
    python -m scripts.create_admin [username]

- username 인자 생략 시 stdin prompt 로 받는다.
- password 는 getpass 로 2회 입력받아 일치 검증.
- email 은 enter 로 skip 가능.
- 이미 동일 username 이 존재하면 exit code 1 + 에러 메시지.
- passlib/bcrypt 해시로 User 행 생성 (is_admin=True).
- get_engine / SessionLocal / init_db 를 그대로 사용 (스크래퍼 CLI 패턴 재사용).
"""
```

핵심: 이 스크립트는 `app.auth.service.create_user(..., is_admin=True)` 를
**그대로 호출**한다 — 로직 중복 금지.

---

## §13. 테스트 설계 (subtask 00021-6 가 구현)

### §13.1 `tests/auth/test_service.py`
- `hash_password` + `verify_password` 왕복.
- `create_user` username 중복 → `DuplicateUsernameError`.
- `issue_session` + `resolve_session` 정상.
- `resolve_session` 만료 (`expires_at` 과거 강제) → None.
- `revoke_session` 후 `resolve_session` → None.
- `validate_username` 허용/거부 표.

### §13.2 `tests/auth/test_routes.py` (FastAPI TestClient)
- POST register → 201/303 + Set-Cookie 존재.
- POST login 실패 시 쿠키 발급 안 됨 + login.html 재렌더 (status 200).
- POST logout → Set-Cookie max-age 0.
- GET `/` 비로그인 200 (회귀 방지 — 핵심).
- GET `/announcements/{id}` 비로그인 200 (회귀 방지 — 핵심).
- GET `/auth/me` 비로그인 → `{"user": null}`.
- GET `/auth/me` 로그인 → `{"user": {...}}`.
- 만료된 쿠키로 GET `/auth/me` → `{"user": null}`.

### §13.3 `tests/auth/test_read_flow.py` — **Phase 1a 리셋 재검증**
사용자 원문 "가짜 User 유닛 테스트를 실사용자 흐름으로 확장":

1. register → login → GET `/` (모든 공고 unread class).
2. GET `/announcements/{id}` → is_read=True (DB 직접 조회로 검증).
3. 다시 GET `/` → 해당 공고 link 클래스가 `--read`.
4. 해당 공고의 title 을 바꾸는 upsert payload 로 scraping 시뮬 호출
   (repository.upsert_announcement 직접 호출 — `action="new_version"` 확인).
5. GET `/` → 해당 공고 (신규 row 의 id) unread 복구 / 기존 id 의
   `AnnouncementUserState` 는 `is_read=False` 로 리셋되어 있는지 확인.

(4) 는 "title 변경" 만으로 충분하다 (Phase 1a 의 `_CHANGE_DETECTION_FIELDS`
에 title 포함).

### §13.4 세션 만료 회귀
`expires_at` 을 과거로 UPDATE 후 `/auth/me` → 401 아님 null. 로그인 페이지로
의 redirect 는 이 task 범위에서는 요구하지 않는다 (비로그인=열람 허용
원칙 고수).

---

## §14. 문서 갱신 (subtask 00021-7)

### §14.1 `README.md`
- §Security 블록 "로컬 전용. FastAPI 백엔드는 인증·권한 제어가 없다." →
  "로컬 전용. 자유 회원가입 기반 세션 인증이 있으나 외부 노출은 금지."
- 퀵스타트 섹션에 로그인·회원가입 한 줄 설명과 /login, /register 경로
  안내.

### §14.2 `README.USER.md`
신규 섹션 "첫 관리자 계정 생성":
```
cd /app
python -m scripts.create_admin
(→ username, password 2회, email 입력)
```
- `is_admin=True` 가 왜 필요한지(관리자 기능 Phase 2 예고) 한 문단.
- 만료된 세션 디버깅 tip (expires_at 수동 SQL 예시).

### §14.3 `PROJECT_NOTES.md`
"인증 없음 — FastAPI 에는 로그인 기능이 없다" 류 기존 문장을 번복하는
것은 **Coder 가 손대지 않는다**. finalize 단계 MemoryUpdater 가 처리.

---

## §15. Subtask 의존 순서 (후속 참고)

```
00021-1 (본 문서)
    ↓
00021-2  §4.1 UserSession ORM, §4.2 상수, §4.3 서비스, §4.4 의존성, §5 헬퍼
    ↓
00021-3  §6 라우트, §7 템플릿(login/register/base nav), §10 CSS auth 부분
    ↓
00021-4  §8 list.html 분기 + §9 detail 자동 읽음 + §10 CSS unread 부분
    ↓
00021-5  §12 scripts/create_admin.py  (§4.3 의 create_user 재사용)
    ↓
00021-6  §13 테스트 전체 (리셋 회귀 + 만료 + 인증 기본)
    ↓
00021-7  §14 문서 갱신
```

병렬화 가능성: 00021-4 는 00021-3 과 독립적으로 템플릿만 건드린다면 같이
가도 되지만, `current_user` 컨텍스트 주입을 00021-3 가 `index_page` /
`detail_page` 에 심어야 하므로 **순차 실행이 안전**.

---

## §16. 불변식 체크리스트 (Reviewer 인용용)

각 subtask Reviewer 가 아래를 확인한다:

- [ ] **비로그인 GET `/` 200** (read_map 빈 dict 경로).
- [ ] **비로그인 GET `/announcements/{id}` 200** (auto-UPSERT 스킵).
- [ ] **비로그인 목록 행의 font-weight 가 기존 500 과 시각적으로 동일**
      (CSS `.is-anonymous` override 로).
- [ ] **로그인 후 안 읽은 공고는 굵게(700), 읽은 공고는 일반(400)**.
- [ ] **상세 진입 후 목록 복귀 시 해당 공고만 normal 로 바뀜**.
- [ ] **공고 title 이 스크래핑으로 바뀌면 기존 is_read 가 False 로 리셋**
      (Phase 1a 로직이 실사용자에서도 동작).
- [ ] **expires_at 을 과거로 바꾸면 `/auth/me` 가 null, 쿠키는 만료 상태**.
- [ ] **create_admin.py 로 생성된 계정의 `is_admin` 이 True**.
- [ ] **POST /auth/* 에서 Origin/Referer 미일치 시 400**.
- [ ] **쿠키: HttpOnly=yes, SameSite=Lax, Secure=no, Path=/**.
- [ ] **세션 기본 수명 30일 (상수 주석으로 근거 명시)**.
