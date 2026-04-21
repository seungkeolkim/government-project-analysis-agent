# 시스템 관리자 운영 가이드

> 이 문서는 **일상 운영·트러블슈팅** 중심이다.
> 프로젝트 개요·설치 방법은 [README.md](README.md) 를 참고한다.

---

## 목차

1. [초기 설치 요약](#초기-설치-요약)
2. [스크래퍼 실행 방법](#스크래퍼-실행-방법)
3. [증분 수집 동작 설명](#증분-수집-동작-설명)
4. [로그 해석](#로그-해석)
5. [DB 관리](#db-관리)
6. [트러블슈팅](#트러블슈팅)
7. [정기 운영 체크리스트](#정기-운영-체크리스트)

---

## 초기 설치 요약

### Docker (권장)

```bash
# 1) 환경변수 파일 생성
cp .env.example .env
# 필요 시 .env 편집 (DB_URL, REQUEST_DELAY_SEC 등)

# 2) 이미지 빌드
docker compose build

# 3) 웹 UI 기동
docker compose up app
# → http://localhost:8000 접속
```

### 로컬 Python (Docker 없이)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env
uvicorn app.web.main:app --host 0.0.0.0 --port 8000
```

---

## 스크래퍼 실행 방법

### 기본 실행 (전체 소스, Docker)

```bash
docker compose --profile scrape run --rm scraper
```

인자를 생략하면 소스당 최대 **10 페이지 · 200 건** 이 수집된다(코드 default).
`sources.yaml` 에 소스별 `max_pages` / `max_announcements` 를 설정하면 그 값이 우선 적용된다.
**CLI 인자는 `docker compose run` 뒤에 바로 붙이면 된다** — `python -m app.cli run` 을 직접 입력하지 않아도 된다.

우선순위: **CLI 인자 > sources.yaml 소스별 설정 > 코드 default (10페이지 / 200건)**

### 기본 실행 (전체 소스, 로컬)

```bash
# 인자 생략 시: 소스당 최대 10페이지 · 200건
python -m app.cli run
```

### 자주 쓰는 옵션

| 옵션 | 설명 | 예시 |
|---|---|---|
| `--max-pages N` | 소스당 최대 페이지 수 (1 이상). 미지정 시 sources.yaml → 코드 default(10) | `--max-pages 3` |
| `--max-announcements N` | 소스당 최대 공고 수 (1 이상). 미지정 시 sources.yaml → 코드 default(200) | `--max-announcements 20` |
| `--skip-detail` | 목록만 수집, 상세 페이지 생략 | `--skip-detail` |
| `--skip-attachments` | 목록·상세 수집은 정상 실행, 첨부파일 다운로드만 생략 | `--skip-attachments` |
| `--dry-run` | DB 쓰기 없이 수집 검증만 | `--dry-run` |
| `--source SOURCE_ID` | 특정 소스만 실행 | `--source IRIS` |
| `--log-level LEVEL` | 로그 레벨 일회성 변경 | `--log-level DEBUG` |

### 활용 예시

**Docker (인자를 run 뒤에 직접 붙임):**

```bash
# 기본값으로 전체 수집 (소스당 최대 10페이지 · 200건)
docker compose --profile scrape run --rm scraper

# 빠른 검증 — 1페이지만 드라이런
docker compose --profile scrape run --rm scraper --max-pages 1 --dry-run

# 페이지·공고 수 직접 지정
docker compose --profile scrape run --rm scraper --max-pages 5 --max-announcements 100

# IRIS만 소량 수집 (상세 생략)
docker compose --profile scrape run --rm scraper --source IRIS --max-pages 2 --skip-detail

# 첨부파일 다운로드 없이 목록·상세만 수집
docker compose --profile scrape run --rm scraper --skip-attachments

# 디버그 로그로 전체 수집
docker compose --profile scrape run --rm scraper --log-level DEBUG
```

**로컬 (python -m app.cli run):**

```bash
# 기본값으로 전체 수집 (소스당 최대 10페이지 · 200건)
python -m app.cli run

# 빠른 검증 — 1페이지만 드라이런
python -m app.cli run --max-pages 1 --dry-run

# 페이지·공고 수 직접 지정
python -m app.cli run --max-pages 5 --max-announcements 100

# 첨부파일 다운로드 없이 목록·상세만 수집
python -m app.cli run --skip-attachments

# 디버그 로그로 전체 수집
python -m app.cli run --log-level DEBUG
```

---

## 증분 수집 동작 설명

스크래퍼를 반복 실행해도 DB를 초기화하지 않는다.
이전 수집 결과를 재사용해 네트워크 요청을 최소화한다.

### 동작 분류

| 상황 | CLI 로그 | DB 동작 |
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

| 실행 방식 | 기본 경로 |
|---|---|
| Docker | `./data/db/app.sqlite3` (호스트 볼륨 마운트) |
| 로컬 | `./data/db/app.sqlite3` |

`.env` 의 `DB_URL` 값으로 변경 가능하다.

### 백업

```bash
# 단순 파일 복사 (SQLite는 파일 하나)
cp ./data/db/app.sqlite3 ./data/db/app.sqlite3.bak.$(date +%Y%m%d)
```

### DB 초기화 (전체 삭제 후 재시작)

```bash
# 데이터 완전 삭제 후 다음 실행 시 자동으로 스키마가 재생성된다
rm -f ./data/db/app.sqlite3
python -m app.cli run --max-pages 1  # 스키마 생성 + 데이터 재수집
```

> **주의**: 삭제 전에 반드시 백업을 먼저 생성한다.

### 스키마 마이그레이션

신규 코드로 업데이트한 후 `init_db` 가 자동으로 마이그레이션을 적용한다.
수동으로 스키마 상태만 확인하려면:

```bash
python -m app.db.init_db
```

### 이력 데이터 정리 (선택)

`is_current=False` 인 구버전 row가 누적될 경우 아래로 정리할 수 있다:

```bash
# 주의: 이 SQL은 되돌릴 수 없다. 백업 후 실행한다.
sqlite3 ./data/db/app.sqlite3 "DELETE FROM announcements WHERE is_current = 0;"
```

---

## 트러블슈팅

### 공고 목록이 비어 있다

1. `python -m app.cli run --max-pages 1 --log-level DEBUG` 로 수집 시도 (Docker: `docker compose --profile scrape run --rm scraper --max-pages 1 --log-level DEBUG`)
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
> 위 증상이 여전히 발생하면 `--log-level DEBUG` 로 실행하여 어떤 공고가 `created`/`new_version`으로 판정되는지 확인한다.

### DB 스키마 오류 (`no such column: is_current`)

기존 DB에 마이그레이션이 적용되지 않은 경우다.
다음 명령으로 마이그레이션을 강제 실행한다:

```bash
python -m app.db.init_db
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
   - Docker: 이미지 재빌드 (`docker compose build`) 후 `playwright install chromium` 스텝 로그 확인
   - 로컬: `playwright install chromium` 실행
4. `--skip-attachments` 플래그 없이 재실행하면 이전에 실패한 항목을 재시도한다.

### 웹 UI에서 첨부파일 다운로드 링크가 404를 반환한다

`stored_path` 가 가리키는 파일이 실제로 존재하지 않는 경우다.
CLI를 재실행해 파일을 다운로드하거나, DB의 해당 `attachments` 레코드가 유효한지 확인한다:

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

- [ ] `python -m app.db.init_db` 로 마이그레이션 적용 확인
- [ ] `docker compose --profile scrape run --rm scraper --max-pages 1 --dry-run` 으로 기본 동작 확인
  - 로컬: `python -m app.cli run --max-pages 1 --dry-run`
- [ ] 웹 UI 로그인 페이지 정상 렌더링 확인
