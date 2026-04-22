# 시스템 관리자 운영 가이드

> 이 문서는 **일상 운영·트러블슈팅** 중심이다.
> 프로젝트 개요·설치 방법은 [README.md](README.md) 를 참고한다.
>
> **Docker 전용.** 호스트에서 `python -m app.cli` 를 직접 실행하는 방식은 지원하지 않는다.

---

## 목차

1. [초기 설치 요약](#초기-설치-요약)
2. [첫 관리자 계정 생성](#첫-관리자-계정-생성)
3. [스크래퍼 실행 방법](#스크래퍼-실행-방법)
4. [NTIS 수집 운영 특이사항](#ntis-수집-운영-특이사항)
5. [증분 수집 동작 설명](#증분-수집-동작-설명)
6. [웹 UI 검색/필터/중복 그룹 보기](#웹-ui-검색필터중복-그룹-보기)
7. [로그 해석](#로그-해석)
8. [DB 관리](#db-관리)
9. [트러블슈팅](#트러블슈팅)
10. [정기 운영 체크리스트](#정기-운영-체크리스트)

---

## 초기 설치 요약

```bash
# 1) 환경변수 파일 생성
cp .env.example .env
# 필요 시 .env 편집 (DB_URL, REQUEST_DELAY_SEC 등)

# 2) sources.yaml 생성 (template 에서 복사, 이미 존재하면 덮어쓰지 않음)
sh scripts/bootstrap_sources.sh
# 필요 시 sources.yaml 편집 (수집 소스·페이지 수 등)
# sources.yaml 은 .gitignore 대상 — 브랜치 전환 시 로컬 수정이 보존된다

# 3) 이미지 빌드
docker compose build

# 4) 웹 UI 기동
docker compose up app
# → http://localhost:8000 접속
```

---

## 첫 관리자 계정 생성

Phase 1b 에서 자유 회원가입 + 세션 쿠키 인증이 추가되었다. 일반 사용자는
`/register` 폼에서 직접 가입할 수 있지만, **관리자(`is_admin=True`) 계정은
DB 컬럼만 존재하고 가입 폼으로는 만들 수 없다**. 운영자가 컨테이너 안에서
`scripts/create_admin.py` CLI 로 한 번 만들어 둔다.

```bash
# 대화형 (username/password/email 모두 prompt)
docker compose run --rm app python scripts/create_admin.py

# username 만 인자로 전달, 나머지는 prompt
docker compose run --rm app python scripts/create_admin.py root_user

# username + email 까지 인자로, password 만 prompt
docker compose run --rm app python scripts/create_admin.py root_user --email admin@example.com
```

**동작 요약**:

- 비밀번호는 항상 `getpass` 로 입력받아 두 번 일치 확인 (터미널에 표시되지 않음).
- bcrypt 해시(라운드 12)로 저장. 평문 저장 없음.
- 같은 username 이 이미 있으면 종료 코드 1 + 에러 메시지.
- 정책: username 은 영문 소문자/숫자/밑줄 3~64자, password 는 8자 이상.
- `is_admin=True` 로 생성된 사용자도 일반 로그인(`/login`) 으로 접속 — 관리자
  전용 화면은 Phase 2 부터 추가될 예정이며, 본 단계에서는 DB 의 플래그만 의미를
  갖는다.

**확인**:

```bash
sqlite3 ./data/db/app.sqlite3 \
  "SELECT id, username, is_admin FROM users WHERE is_admin = 1;"
```

> **컨테이너 내부 vs 호스트.** 위 예시는 Docker 사용을 전제로 한다. 호스트에서
> 직접 가상환경을 돌리는 개발 환경이라면 `docker compose run --rm app`
> 부분만 빼고 `python scripts/create_admin.py` 로 동일하게 사용한다.

### 만료된 세션 디버깅

세션 수명은 기본 30 일이다 (`app/auth/constants.py` 의
`SESSION_LIFETIME_DAYS`). 운영 중 임의 사용자의 세션을 즉시 만료시키려면
DB 의 `user_sessions.expires_at` 을 과거로 UPDATE 한다 — 다음 요청부터
해당 사용자는 비로그인으로 처리되며, 자동으로 로그인 페이지에서 다시
인증할 수 있다.

```bash
# 모든 세션 일괄 만료 (운영자 강제 로그아웃)
sqlite3 ./data/db/app.sqlite3 \
  "UPDATE user_sessions SET expires_at = datetime('now', '-1 day');"

# 특정 사용자의 세션만 만료
sqlite3 ./data/db/app.sqlite3 \
  "UPDATE user_sessions
     SET expires_at = datetime('now', '-1 day')
   WHERE user_id = (SELECT id FROM users WHERE username = 'root_user');"
```

만료된 row 자체는 자동으로 삭제되지 않는다 — 적극적인 cleanup 배치는 Phase 2
스케줄러에서 도입될 예정이다.

---

## 스크래퍼 실행 방법

### 기본 실행

```bash
docker compose --profile scrape run --rm scraper
```

모든 실행 파라미터는 `sources.yaml` 의 `scrape:` 섹션으로 제어한다. CLI 인자는 사용하지 않는다.
우선순위: **scrape: 전역 설정 > sources: 소스별 설정 > 코드 default (10페이지 / 200건)**

### 주요 파라미터 (sources.yaml)

| 필드 | 기본값 | 설명 |
|---|---|---|
| `scrape.active_sources` | `[]` | 실행할 소스 ID 목록. 비어 있으면 `enabled: true` 소스 전체 실행 |
| `scrape.max_pages` | `null` | 소스당 최대 페이지 수. null 이면 소스별 설정 → 코드 default(10) |
| `scrape.max_announcements` | `null` | 소스당 최대 공고 수. null 이면 소스별 설정 → 코드 default(200) |
| `scrape.skip_detail` | `false` | true 이면 목록 적재만 수행 (상세 생략) |
| `scrape.skip_attachments` | `false` | true 이면 첨부파일 다운로드 생략 |
| `scrape.dry_run` | `false` | true 이면 DB 쓰기 없이 수집 동작만 검증 |
| `scrape.log_level` | `null` | 로그 레벨 오버라이드. null 이면 .env 의 LOG_LEVEL 사용 |

### 활용 패턴

설정을 변경한 뒤 `docker compose --profile scrape run --rm scraper` 를 실행한다.

**빠른 검증 — 드라이런 (DB 쓰기 없음):**

```yaml
scrape:
  max_pages: 1
  dry_run: true
```

**특정 소스만 실행:**

```yaml
scrape:
  active_sources: [NTIS]
```

**목록·상세만 수집, 첨부파일 다운로드 생략:**

```yaml
scrape:
  skip_attachments: true
```

**목록만 수집 (상세·첨부 모두 생략):**

```yaml
scrape:
  skip_detail: true
```

**디버그 로그로 전체 수집:**

```yaml
scrape:
  log_level: DEBUG
```

**동시 실행.** 같은 명령을 여러 터미널에서 동시에 실행할 수 있다.
각 컨테이너가 `sources.yaml` 의 독립적인 복사본을 사용하므로 설정 경합이 발생하지 않는다.
단, 동일 DB에 동시 쓰기하므로 SQLite WAL 이 활성화되어 있는지 확인한다.

---

## NTIS 수집 운영 특이사항

### 수집 범위 설정

NTIS 마감 공고가 74,000건+ 에 달하므로 `sources.yaml` 기본값을 보수적으로 설정했다 (5페이지 · 100건).
전체 수집이 필요하다면 `sources.yaml` 의 NTIS 소스 또는 전역 scrape 섹션을 수정한다:

```yaml
# sources.yaml
scrape:
  active_sources: [NTIS]
  max_pages: 50
  max_announcements: 5000
```

> **주의**: 위 설정은 수집에 오랜 시간이 걸린다. 실행 후 반드시 터미널을 유지하거나 nohup 등을 사용한다.

NTIS `request_delay_sec` 기본값은 2.0초다. 짧게 줄이면 차단 위험이 있다.

### 첨부파일 다운로드

NTIS 첨부파일은 **httpx POST** 직접 다운로드를 사용한다 (IRIS의 Playwright 경로와 다름).
Playwright 브라우저가 미설치된 환경에서도 NTIS 첨부파일은 정상 다운로드된다.

### canonical 매칭 (cross-source 중복 공고)

NTIS 목록 수집 시 공식 공고번호(ancmNo)를 알 수 없어 **fuzzy canonical key** 가 먼저 부여된다.
상세 수집 완료 후 공고번호가 확보되면 자동으로 **official canonical key** 로 승급된다.
IRIS와 동일 공고인 경우 같은 `canonical_group_id` 로 묶인다.

canonical 승급이 이뤄진 경우 로그에 아래 메시지가 출력된다:
```
INFO  canonical 재계산 완료(fuzzy→official): source=NTIS id=… ancm_no=…
```

---

## 증분 수집 동작 설명

스크래퍼를 반복 실행해도 DB를 초기화하지 않는다.
이전 수집 결과를 재사용해 네트워크 요청을 최소화한다.

### 동작 분류

| 상황 | 로그 | DB 동작 |
|---|---|---|
| 신규 공고 | `신규 공고 등록` (INFO) | 새 row INSERT |
| 변경 없음 + 상세 있음 | `상세 수집 생략(변경 없음, 기존 데이터 재사용)` (INFO) | 변경 없음 |
| 변경 없음 + 상세 없음 | `변경 없음` (DEBUG) → 상세 수집 진행 | 상세 필드만 UPDATE |
| 내용 변경 (공고명/마감일 등) | `내용 변경 — 신규 버전 등록` (INFO) | 구버전 `is_current=False` 봉인, 신규 row INSERT |
| 상태 전이만 | `상태 전이 — in-place 갱신` (INFO) | 기존 row 상태 UPDATE |

### 변경 감지 비교 대상 필드

- **공고명(title)**, **상태(status)**, **마감일(deadline_at)**, **기관명(agency)**
- `received_at`(접수시작일)은 비교 대상에서 제외 (접수예정 상태에서 미기재→보완 패턴이 빈번)

### 이력 조회 (SQL)

```sql
-- 현재 유효 버전만 (목록 UI와 동일)
SELECT * FROM announcements WHERE is_current = 1;

-- 특정 공고의 전체 이력 (구버전 포함)
SELECT * FROM announcements
WHERE source_type = 'IRIS' AND source_announcement_id = '12345'
ORDER BY id;
```

---

## 웹 UI 검색/필터/중복 그룹 보기

웹 UI(`http://localhost:8000`)는 GET 쿼리 파라미터로 필터/정렬/페이지 상태를 보존한다.

### 쿼리 파라미터

| 파라미터 | 허용값 | 기본값 | 설명 |
|---------|--------|--------|------|
| `status` | `접수중` `접수예정` `마감` 또는 생략 | 전체 | 공고 상태 필터 |
| `source` | `IRIS` `NTIS` 등 소스 ID 또는 생략 | 전체 | 수집 소스 필터 |
| `search` | 임의 문자열 | - | 제목 부분 일치 검색 |
| `sort` | `received_desc` `deadline_asc` `title_asc` | `received_desc` | 정렬 기준 |
| `group` | `on` `off` | `off` | 중복 묶어 보기 토글 |
| `page` | 정수 | `1` | 페이지 번호 |

### 예시 URL

```
# 접수중 공고만, 마감일 가까운순
http://localhost:8000/?status=접수중&sort=deadline_asc

# NTIS 소스, 제목에 "나노" 포함
http://localhost:8000/?source=NTIS&search=나노

# 중복 묶어 보기 + 2페이지
http://localhost:8000/?group=on&page=2
```

### 중복 묶어 보기 (`group=on`)

같은 과제가 IRIS·NTIS 양쪽에 등록된 경우 1행으로 묶어 표시한다.
행 우측 배지(예: `3건`)를 클릭하면 소스별 개별 공고를 펼쳐볼 수 있다.
기본값(`group=off`)은 소스별로 각각 1행 표시하며, `동일 과제 N건` 배지로 중복 여부를 안내한다.

---

## 로그 해석

### 정상 수집 시 주요 로그

```
INFO  목록 수집 시작: source=IRIS max_pages=10
INFO  목록 수집 완료: source=IRIS 42건
INFO  [1/42] 공고 처리: source=IRIS id=12345
INFO  신규 공고 등록: source=IRIS id=12345
INFO  상세 수집 완료(ok): source=IRIS id=12345
INFO  첨부 수집 완료: source=IRIS id=12345 성공=3 실패=0 생략(이미 존재)=0
...
INFO  [5/42] 공고 처리: source=IRIS id=11111
INFO  상세 수집 생략(변경 없음, 기존 데이터 재사용): source=IRIS id=11111
INFO  첨부 수집 완료: source=IRIS id=11111 성공=0 실패=0 생략(이미 존재)=2
...
INFO  소스 IRIS 완료: 목록 성공 42건 / 실패 0건 | 상세 성공 5건 / 실패 0건 / 생략(변경없음) 37건 | 첨부 성공 8건 / 실패 0건 / 생략 74건 | action 분포: 신규=5 변경없음=37 버전갱신=0 상태전이=0
INFO  scrape 실행 완료: 목록 성공 42건 / 목록 실패 0건 | 상세 성공 5건 / 실패 0건 / 생략(변경없음) 37건 | 첨부 성공 8건 / 실패 0건 / 생략 74건 | action 분포: 신규=5 변경없음=37 버전갱신=0 상태전이=0
```

> **확인 포인트**: 2회차 이후 실행에서 `변경없음=N` 이 전체 공고 수와 가까울수록 정상이다.
> `신규=0 변경없음=42` 이면 모든 공고가 기존 데이터를 재사용하고 상세·첨부 재수집도 최소화된다.
> 첨부는 sha256 비교로 중복 방지 — 이미 존재하는 파일은 `생략(이미 존재)` 으로 집계된다.

### 주의가 필요한 로그

| 로그 | 의미 | 대응 |
|---|---|---|
| `INFO 상태 전이 — in-place 갱신` | 동일 공고가 다른 상태로 재등장해 DB 상태 갱신 | 정상 동작. 빈번하게 발생한다면 `docs/status_transition_todo.md` 참고 |
| `WARNING detail_url 없음 — 상세 수집 스킵` | 목록에서 상세 URL을 추출하지 못함 | 해당 소스의 HTML 구조 변경 여부 확인 |
| `ERROR 공고 upsert 실패` | DB 쓰기 실패 | DB 파일 권한·디스크 용량 확인 |
| `목록 실패 공고 ID 목록` | 특정 공고 처리 실패 | 로그 상세 확인 후 재실행 |

---

## DB 관리

### DB 파일 위치

기본 경로: `./data/db/app.sqlite3` (호스트 볼륨 마운트)

`.env` 의 `DB_URL` 값으로 변경 가능하다.

### 백업

#### 자동 백업 스크립트 (권장)

일상 운영에서는 `scripts/backup_db.py` 를 사용한다 — SQLite 온라인 백업
(`sqlite3.connect().backup()`) 을 수행하므로 스크래퍼 실행 중에도 일관된
스냅샷이 만들어진다.

```bash
# 기본: data/backups/ 에 UTC 타임스탬프 파일명으로 저장, 최근 14개 보관
docker compose run --rm scraper python scripts/backup_db.py

# 보관 개수 / 저장 위치 변경 예
docker compose run --rm scraper python scripts/backup_db.py --keep 30
docker compose run --rm scraper python scripts/backup_db.py --dest /mnt/backups
```

**동작 요약**:

- 저장 위치: `./data/backups/` (기본, `--dest` 로 변경 가능)
- 파일명: `app.sqlite3.YYYYMMDDThhmmssZ.bak` (UTC 타임스탬프, 충돌 없음)
- 보관 정책: mtime 내림차순 정렬 후 최근 `--keep` 개(기본 **14**)만 유지,
  나머지는 자동 삭제 (= 매일 1회 실행 시 약 2주간 복원 지점 보장)
- `DB_URL` 이 `sqlite:///` 가 아니면(예: Postgres) INFO 로그만 남기고 skip
  (종료 코드 0)

**정기 실행 권장**: 스크래퍼 실행 직전 또는 직후, 호스트 cron / systemd timer
에서 하루 1회 실행을 권장한다.

```bash
# 예: /etc/cron.d/gov-project-backup
0 2 * * * cd /path/to/repo && docker compose run --rm scraper python scripts/backup_db.py >> /var/log/gov-backup.log 2>&1
```

#### 복원 절차

> **주의**: 복원 전 웹 서버(`app` 서비스)를 먼저 중지해 파일 잠금을 해제한다.

```bash
# 1) 웹 서버 중지 (스크래퍼 실행 중이면 종료 대기)
docker compose down app

# 2) 복원하려는 백업을 운영 경로로 덮어쓰기
cp ./data/backups/app.sqlite3.20260422T150000Z.bak ./data/db/app.sqlite3

# 3) 웹 서버 재기동
docker compose up app
```

복원 직후 웹 UI 에서 `/` 페이지가 정상 렌더링되는지 확인한다.

#### 수동 파일 복사 (소량·임시)

개발 단계나 일회성 백업은 그대로 `cp` 를 써도 된다 (SQLite 는 단일 파일):

```bash
cp ./data/db/app.sqlite3 ./data/db/app.sqlite3.bak.$(date +%Y%m%d)
```

다만 스크래퍼가 실행 중이면 WAL 로그와의 일관성이 깨질 수 있으므로 자동
스크립트를 권장한다.

> **Postgres 전환 시**: `scripts/backup_db.py` 는 SQLite 전용이다.
> Postgres 로 전환한다면 `pg_dump` / `pg_basebackup` 등 Postgres 공식 도구를
> 별도로 운영한다 (이 스크립트는 skip 으로 빠진다).

### DB 초기화 (전체 삭제 후 재시작)

```bash
# 데이터 완전 삭제 후 다음 실행 시 자동으로 스키마가 재생성된다
rm -f ./data/db/app.sqlite3
# sources.yaml 에서 max_pages: 1 설정 후 실행하면 스키마 생성 + 데이터 재수집
docker compose --profile scrape run --rm scraper
```

> **주의**: 삭제 전에 반드시 백업을 먼저 생성한다.

### 스키마 마이그레이션 (Alembic)

신규 코드로 업데이트한 후 컨테이너 기동 시 **자동으로** Alembic migration 이 적용된다.
별도 명령이 필요 없다.

**적용 전략 (자동 분기)**

| DB 상태 | 전략 | 효과 |
|---------|------|------|
| 빈 DB | `upgrade head` | baseline 스키마 전체 생성 |
| 기존 DB (Alembic 도입 전) | `stamp head` | 데이터 무변경, 리비전 레코드만 삽입 |
| Alembic 관리 DB | `upgrade head` | 신규 migration 적용, 없으면 no-op |

수동으로 Alembic 상태를 확인하려면:

```bash
# 현재 적용된 리비전 확인
docker compose run --rm scraper alembic current

# 적용 이력 확인
docker compose run --rm scraper alembic history --verbose

# 수동 upgrade (자동 적용 실패 시)
docker compose run --rm scraper alembic upgrade head
```

### canonical backfill (일회성)

기존 수집 데이터에 canonical_group_id가 채워지지 않은 경우(00013 적용 이전 수집분) 한 번 실행한다.
이미 채워진 row는 건너뛰므로 **멱등** — 실수로 두 번 실행해도 안전하다.

```bash
# 1) dry-run 으로 대상 건수 확인 (DB 변경 없음)
docker compose run --rm scraper python scripts/backfill_canonical.py --dry-run

# 2) 실제 실행 (200건마다 commit)
docker compose run --rm scraper python scripts/backfill_canonical.py --batch-size 200
```

신규 DB(00013 이후 설치)는 첫 수집 시부터 자동으로 canonical이 채워지므로 이 스크립트를 실행하지 않아도 된다.

### 이력 데이터 정리 (선택)

`is_current=False` 인 구버전 row가 누적될 경우 아래로 정리할 수 있다:

```bash
# 주의: 이 SQL은 되돌릴 수 없다. 백업 후 실행한다.
sqlite3 ./data/db/app.sqlite3 "DELETE FROM announcements WHERE is_current = 0;"
```

---

## 트러블슈팅

### 기동 시 "sources.yaml 마운트 없음 — template 기본값으로 기동합니다" 경고가 출력된다

호스트 루트에 `sources.yaml` 이 없는 경우 entrypoint 가 이미지 내 template 을 폴백으로 사용해
계속 기동한다. 수집 파라미터를 실제 설정으로 반영하려면 아래 순서로 진행한다:

```bash
# 1) sources.yaml 생성
sh scripts/bootstrap_sources.sh

# 2) 필요 시 편집
vim sources.yaml   # 또는 선호하는 에디터

# 3) 컨테이너 재기동 (바인드 마운트가 갱신된 파일을 읽는다)
docker compose restart app
```

> `sources.yaml` 은 `.gitignore` 대상이므로 저장소에 포함되지 않는다.
> 처음 clone 한 후 또는 `./data` 를 초기화한 후에 이 경고가 나타날 수 있다.

### 공고 목록이 비어 있다

1. `sources.yaml` 에서 `scrape.log_level: DEBUG`, `scrape.max_pages: 1` 설정 후
   `docker compose --profile scrape run --rm scraper` 실행하여 수집 로그 확인
2. `목록 수집 완료: source=IRIS 0건` 이면 IRIS 사이트 응답 이상 → 브라우저에서 직접 확인
3. DB에 데이터는 있는데 웹 UI에 안 보이면 → `is_current=1` 조건 확인

```sql
SELECT COUNT(*) FROM announcements WHERE is_current = 1;
```

### 재실행해도 상세 수집이 계속 발생한다

- 종료 로그의 `action 분포: 신규=N 변경없음=N` 에서 `변경없음` 이 0에 가까우면 변경 감지가 오동작 중이다.
- `상세 수집 생략` 로그가 없고 매번 상세를 수집하면:
  - `detail_fetched_at` 이 NULL인 row가 있는지 확인 (상세가 아직 한 번도 수집 안 된 경우)
  - 비교 필드(title/status/deadline_at/agency)가 매 수집마다 달라지는지 확인

```sql
SELECT id, title, deadline_at, detail_fetched_at
FROM announcements WHERE is_current = 1
ORDER BY id LIMIT 10;
```

> **[00007 이후]** deadline_at tz-naive/aware 불일치 및 문자열 공백 차이로 인한 false-positive 변경 감지가 수정됐다.
> 위 증상이 여전히 발생하면 `scrape.log_level: DEBUG` 로 실행하여 어떤 공고가 `created`/`new_version`으로 판정되는지 확인한다.

### DB 스키마 오류 (`no such column: is_current`)

기존 DB에 Alembic migration 이 적용되지 않은 경우다.
다음 명령으로 수동 적용한다:

```bash
docker compose run --rm scraper alembic upgrade head
```

그래도 해결되지 않으면 DB를 백업 후 삭제해 새로 생성한다.

### Docker 컨테이너에서 권한 오류

`./data/` 디렉터리의 소유자/권한을 확인한다:

```bash
ls -la ./data/
chmod -R 755 ./data/
```

### 로그에 `상태 전이 — in-place 갱신` 이 자주 나타난다

접수예정·접수중·마감 3개 상태를 순차 수집하므로 동일 공고가 다른 상태로 재등장하면 정상적으로 발생한다.
비정상적으로 많은 경우(예: 매 실행마다 같은 공고가 계속 상태 전이로 잡히는 경우) `docs/status_transition_todo.md` 를 참고한다.

### 첨부파일이 다운로드되지 않는다

1. 로그에서 `첨부 수집` 관련 라인을 확인한다.
2. `attachment_errors` 키가 있는지 DB에서 확인한다:
   ```sql
   SELECT id, source_announcement_id, json_extract(raw_metadata, '$.attachment_errors')
   FROM announcements
   WHERE is_current = 1 AND raw_metadata LIKE '%attachment_errors%';
   ```
3. Playwright 브라우저가 설치되어 있는지 확인한다:
   - `docker compose build` 후 `playwright install chromium` 스텝 로그 확인
4. `sources.yaml` 에서 `scrape.skip_attachments: false` 설정 후 재실행하면 이전에 실패한 항목을 재시도한다.

### 웹 UI에서 첨부파일 다운로드 링크가 404를 반환한다

`stored_path` 가 가리키는 파일이 실제로 존재하지 않는 경우다.
스크래퍼를 재실행해 파일을 다운로드하거나, DB의 해당 `attachments` 레코드가 유효한지 확인한다:

```sql
SELECT id, original_filename, stored_path FROM attachments
WHERE announcement_id = {공고_id};
```

---

## 정기 운영 체크리스트

### 매 수집 후 확인

- [ ] 종료 코드 0 확인 (`echo $?`)
- [ ] `scrape 실행 완료` 로그에서 `목록 실패` 건수 = 0 확인
- [ ] `scrape 실행 완료` 로그에서 `첨부 실패` 건수 확인 (0이면 정상)
- [ ] 웹 UI(`http://localhost:8000`) 에서 최신 공고 표시 확인

### 주간 확인

- [ ] DB 파일 크기 확인 (`ls -lh ./data/db/app.sqlite3`)
- [ ] 구버전 row 누적 확인 (`SELECT COUNT(*) FROM announcements WHERE is_current=0;`)
- [ ] 로그에 반복 `WARNING` 메시지 없는지 확인

### 업데이트 후 확인

- [ ] `docker compose build` 로 이미지 재빌드
- [ ] `docker compose run --rm scraper alembic current` 로 리비전이 `head` 임을 확인
- [ ] `sources.yaml` 에서 `scrape.dry_run: true`, `scrape.max_pages: 1` 설정 후
      `docker compose --profile scrape run --rm scraper` 로 기본 동작 확인
- [ ] 웹 UI 목록 페이지 정상 렌더링 확인
