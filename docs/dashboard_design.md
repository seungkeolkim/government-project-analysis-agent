# Phase 5b — 대시보드 UI 설계 노트

> **작성 범위**: Task 00042 (Phase 5b) — Phase 5a (00041) 가 완성한
> `scrape_snapshots` 테이블 + payload 5종 카테고리 + `merge_snapshot_payload`
> 머지 함수 위에 두 시점 비교 + 1개월 이내 활성 공고 + 사용자 라벨링 위젯 +
> ±15일 추이 차트를 가진 비로그인 가능 `GET /dashboard` 페이지를 짓는다.
>
> 본 문서는 `docs/snapshot_pipeline_design.md` / `docs/relevance_ui_design.md`
> / `docs/favorites_ui_design.md` 등과 동일한 한국어·markdown 톤을 따른다.
> 후속 subtask (00042-2 ~ 00042-7) 가 코드/PR 설명에서 인용할 수 있도록 § 번호와
> 헤더를 안정적으로 부여한다 — **이 문서가 살아 있는 동안 § 번호는 변경하지
> 않는다**. 새 절은 §15 이후로 append.
>
> 구현 본문(실제 함수 바디)은 포함하지 않는다. 각 모듈의 시그니처(이름 +
> 파라미터 + 반환 타입) 수준만 기술한다.

---

## §1. 스코프와 전제

### §1.1 이 task 에서 다루는 것 (5b)

- 새 라우트 `GET /dashboard` (비로그인 가능) — 좌상단 네비에 "대시보드" 탭 추가.
- 컨트롤 영역 — 기준일 캘린더 + 비교 대상 드롭다운(전날 / 전주 / 전월 / 전년 /
  직접 선택) + 비교일 캘린더(직접 선택 시).
- A 섹션 (공고의 변화) — `(from, to]` 구간의 모든 ScrapeSnapshot.payload 를
  시간순 누적 머지한 결과를 5종 카테고리 카드로 표시하고, 카드 클릭 시 전체
  리스트를 expand 한다.
- B 섹션 (조만간 변화 예정) — `to` 시점 기준 향후 30일 이내에 접수 시작 또는
  마감이 예정된 `is_current=True` 공고를 DB select 로 조회한다.
- 사용자 라벨링 위젯 4종 — 로그인 시에만 렌더 + 쿼리 (전체 미확인 공고 / 전체
  미판정 관련성 / 기준일 변경 공고 중 미확인 / 기준일 변경 공고 중 미판정).
- 추이 차트 — 기준일 ±15일 (총 30일) 일별 신규 / 내용 변경 / 전이 카운트 line
  chart. Chart.js 로컬 vendor 번들 사용 (외부 CDN 금지 컨벤션).
- 신규 헬퍼 4종 + snapshot 가용 날짜 헬퍼 — N+1 회피 IN 쿼리 패턴 (Phase 1b /
  3a / 3b 동일 패턴 재사용).
- snapshot 가용 날짜 JSON API `GET /dashboard/api/snapshot-dates`.
- 회귀 테스트 `tests/dashboard/test_dashboard_routes.py` — 사용자 원문 검증
  17 항목 회귀.
- 문서: 본 문서, README.USER.md "대시보드 사용법" 섹션 신설, LICENSE / NOTICE
  의 Chart.js MIT 라이선스 추가.

### §1.2 이 task 에서 다루지 않는 것 (범위 밖)

- snapshot 자동 새로고침 (SSE / polling). 사용자가 reload 해야 반영된다.
- 대시보드 내 사용자 액션 (mark read / 즐겨찾기 / 관련성 판정) — 대시보드는
  read-only 다. 기존 목록 / 상세 페이지의 기존 동작을 그대로 활용한다.
- 다중 비교 (3개 이상 시점).
- CSV / 엑셀 export.
- 대시보드 데이터 캐싱.
- 차트 인터랙션 (zoom / brush / hover crosshair 등).
- 첨부 분석 결과 표시.
- `to` 가 과거인 경우의 활성 공고를 그 시점 기준으로 정확하게 재현하기 — 이력
  row (`is_current=False` 봉인된 row) 활용은 본 task 범위 밖이며, "현재 기준이며
  정확하지 않을 수 있습니다" 안내문으로 처리한다.

### §1.3 절대 건드리지 않는 것 (회귀 금지)

- `app/db/snapshot.py` 의 머지 함수 — `merge_snapshot_payload(existing, new)`
  / `build_snapshot_payload(apply_result)` / `normalize_payload(payload)` 의
  시그니처와 머지 규칙. 신규 머지 함수를 만들지 않는다 (사용자 원문 주의사항
  "신규 머지 함수 금지"). `(from, to]` 구간의 N일치 누적 머지는 본 함수를
  외부에서 두 payload 합치는 의미로 그대로 reduce 한다 (§5).
- `app/timezone.py` 의 KST 헬퍼 — `to_kst` / `now_kst` / `now_utc` /
  `format_kst` / `kst_date_boundaries(date)`. `datetime.utcnow()` /
  naive `datetime.now()` 직접 사용 금지(Phase 4 컨벤션).
- Jinja2 KST 필터 — 모든 timestamp 표시는 `kst_format` / `kst_date` 필터 경유
  (`app/web/template_filters.py` 등록). 본 task 에서 새 필터를 만들지 않는다.
- `app/cli.py` 의 ScrapeRun completed / partial 분기 — `build_snapshot_payload`
  + `upsert_scrape_snapshot` 호출 흐름 (line 1247–1252). 본 task 는 read-only
  소비자이므로 수집 / 머지 / GC 동작에 영향을 주지 않는다 (검증 17 의 "5a 회귀
  영향 없음").
- `announcements` / `attachments` / `scrape_snapshots` / `relevance_judgments` /
  `announcement_user_states` 테이블 DDL — 본 task 의 migration 은 **없다**.
- Phase 1b 의 `current_user_optional` Depends + 비로그인 분기 컨벤션. 대시보드
  도 동일 dependency 를 사용한다.

---

## §2. 5a 동작 검증 (캘린더 가용 날짜 판정 정책의 근거)

본 절은 modify v2 가이드의 "5a 동작을 코드로 직접 확인 후 docs 에 박는다"
요건을 충족시키기 위한 코드 인용 절이다. 사용자 원문 modify 메시지 문구
(\"변화 0건 수집 시 5a 가 snapshot 을 생성 / 갱신하지 않는 현재 동작\") 와 실제
코드의 동작이 일치하는지 한 번 더 검증해 둔다.

### §2.1 cli.py 의 snapshot UPSERT 호출 분기

`app/cli.py:1216–1257` 발췌 (요지):

```python
if candidate_status == "cancelled":
    # SIGTERM — apply 자체를 건너뜀. 본 테이블 / snapshot 변경 없음.
    ...
else:
    # completed / partial — apply 트랜잭션 단일 진입.
    with session_scope() as apply_session:
        apply_result = apply_delta_to_main(apply_session, scrape_run_id=...)
        snapshot_payload = build_snapshot_payload(apply_result)
        upsert_scrape_snapshot(
            apply_session,
            snapshot_date=now_kst().date(),
            new_payload=snapshot_payload,
        )
```

핵심:

- ScrapeRun 의 최종 status 가 `completed` 또는 `partial` 이면 **변화 0건이든
  아니든 무조건** `build_snapshot_payload` + `upsert_scrape_snapshot` 가 호출
  된다. "변화가 있을 때만 부른다" 는 분기는 존재하지 않는다.
- ScrapeRun 의 최종 status 가 `cancelled` (SIGTERM) / `failed` (apply 트랜잭션
  자체가 raise) 이면 snapshot UPSERT 가 일어나지 않는다.

### §2.2 build_snapshot_payload — 빈 입력의 정규형 반환

`app/db/snapshot.py:135–194`:

- `apply_result.new_announcement_ids` / `content_changed_announcement_ids` /
  `transitions` 가 모두 빈 컬렉션이어도 함수는 "5종 카테고리 빈 배열 +
  counts={..:0}" 인 정규형 dict 를 반환한다 (`_build_counts(payload)`).
- 즉 5종이 모두 0 인 빈 payload 가 생성된다.

### §2.3 upsert_scrape_snapshot — 빈 payload 도 INSERT

`app/db/repository.upsert_scrape_snapshot` (3545–3613) docstring 인용:

> 빈 ScrapeRun (5종 카테고리 모두 빈 배열) 처리:
> 설계 §10.3 권장안에 따라 신규 INSERT 시에도 row 를 만든다 — 같은 날의
> 후속 ScrapeRun 이 머지할 대상이 되며, 5b 의 캘린더가 "이 날 수집 시도가
> 있었음" 을 표시할 수 있다.

본문 동작 (`existing_row is None` 분기):

```python
if existing_row is None:
    normalized = normalize_payload(new_payload)  # 5종 빈 배열 + counts=0 채움
    snapshot = ScrapeSnapshot(snapshot_date=snapshot_date, payload=normalized)
    session.add(snapshot)
    session.flush()
```

조건 분기 없음 — `new_payload.counts` 합산이 0 이어도 INSERT 한다.

### §2.4 normalize_payload — 누락 카테고리를 빈 컨테이너로 채움

`app/db/snapshot.normalize_payload` (67–127) — 호출 시 빈 `dict()` 또는 `None`
을 받아도 5종 카테고리를 빈 list 로 채워 정규형을 반환하며, `counts` 도 5종
모두 0 으로 채운다.

### §2.5 결론 — 사용자 원문 모델과 실제 코드의 차이

| 사용자 원문 modify v2 가정 | 실제 5a 코드 동작 |
| --- | --- |
| 변화 0건 수집 시 snapshot **미생성** | ScrapeRun 이 completed / partial 로 끝나면 변화 0건이어도 snapshot row **생성** (UPSERT) |
| "수집했지만 변화 0건" 인 날은 캘린더에서 비활성 | "수집했지만 변화 0건" 인 날도 캘린더에서 **활성** (snapshot row 가 있음) |

Phase 5a 의 설계 의도는 `docs/snapshot_pipeline_design.md §10.3` 에서 이미
**의도적**으로 못박혀 있다 — "5b 의 캘린더가 '이 날 수집 시도가 있었음' 을
표시할 수 있도록".

→ 본 task 는 **5a 의 실제 코드 동작을 정답** 으로 채택하고 (§4.1 가용 날짜
판정 정책), 사용자 원문의 modify v2 가정은 docs 안에서 명시적으로 정정한다.

---

## §3. 페이지 레이아웃 와이어프레임

비로그인 / 로그인 두 경우 모두 같은 골격이지만 §3.2 의 "2. 사용자 라벨링 위젯"
영역만 비로그인 시 통째로 skip 된다 (DOM 에 렌더되지 않음 + 쿼리도 호출되지
않음 — §8.3).

### §3.1 페이지 골격 (위 → 아래 5 영역)

```
+------------------------------------------------------------+
| header / site-nav (base.html) … [대시보드] [즐겨찾기] [user]|
+------------------------------------------------------------+
| 1. 컨트롤                                                  |
|   ┌──────────────┐ 비교: ▼ 전날             ┌──────────┐  |
|   │ 기준일 캘린더 │     (전주/전월/전년/직접) │ 비교일   │  |
|   │ 2026-04 …    │                          │ 캘린더   │  |
|   └──────────────┘                          └──────────┘  |
|   (직접 선택일 때만 비교일 캘린더 표시)                   |
+------------------------------------------------------------+
| 2. 사용자 라벨링 위젯 (로그인 시만)                        |
|   [전체 미확인 N건] [전체 미판정 M건]                     |
|   [기준일 미확인 K건] [기준일 미판정 L건]                 |
+------------------------------------------------------------+
| 3. A. 공고의 변화 (from, to] 누적 diff                     |
|   ┌─신규────────┐ ┌─내용 변경──┐ ┌─→ 접수예정┐ ┌─→ 접수중┐ ┌─→ 마감┐|
|   │  N건  ↑X    │ │  N건  ↑X   │ │  N건 ↑X  │ │  N건 ↑X │ │  N건 │|
|   │  (비교 M건) │ │  (비교 M건)│ │  (비교 M건)│ │ (비교) │ │ (비교)│|
|   └─────────────┘ └────────────┘ └──────────┘ └─────────┘ └──────┘|
|   (카드 클릭 시 expand → 전체 리스트, §6.2 모킹)          |
|   * fallback 안내문이 있으면 카드 위에 노란 박스          |
+------------------------------------------------------------+
| 4. B. 조만간 변화 예정 (to 기준 향후 30일)                 |
|   ┌─조만간 접수────┐  ┌─조만간 마감────┐                  |
|   │ 리스트 (스크롤)│  │ 리스트 (스크롤)│                  |
|   └────────────────┘  └────────────────┘                  |
|   * to 가 과거이면 회색 박스 안내문 (§7.2)                |
+------------------------------------------------------------+
| 5. 추이 차트 (기준일 ±15일 일별 카운트, line chart)        |
|   ┌────────────────────────────────────────────────────┐  |
|   │ 신규 / 내용 변경 / 전이 3 series, x: %m-%d (KST)   │  |
|   └────────────────────────────────────────────────────┘  |
+------------------------------------------------------------+
| footer (base.html)                                         |
+------------------------------------------------------------+
```

### §3.2 비로그인 / 로그인 차이

- 비로그인: 영역 1 / 3 / 4 / 5 만 렌더. 영역 2 는 `{% if current_user %} … {% endif %}`
  로 통째로 skip 되어 DOM 에도 들어가지 않는다. 라우트도 §8.3 에서 쿼리 자체를
  스킵한다 — DEBUG 로그로 확인 가능 (검증 16).
- 로그인: 영역 1–5 모두 렌더. 위젯 4 종 모두 카운트 표시.

---

## §4. snapshot 가용성 fallback 정책

### §4.1 캘린더 가용 날짜 판정 정책 (modify v2 추가 항목)

**판정 기준**: `scrape_snapshots.snapshot_date` UNIQUE 컬럼에 row 가 존재
하느냐 한 가지로 결정한다. payload 의 counts 합산이 0 인 row 도 **활성**
으로 본다.

**근거** (§2.3 / §2.5 의 코드 인용):

- 5a 의 cli.py 가 ScrapeRun completed / partial 종료 시 변화 0건이어도
  `upsert_scrape_snapshot` 을 무조건 호출한다.
- `upsert_scrape_snapshot` 은 빈 payload 도 INSERT 한다 (조건 분기 없음).
- `docs/snapshot_pipeline_design.md §10.3` 가 "5b 의 캘린더가 '이 날 수집
  시도가 있었음' 을 표시할 수 있도록" 을 의도로 명시.

**결론 — 디자인 의도**:

- 캘린더의 활성 / 비활성은 "**그 날 변화 카운트가 0 보다 컸느냐**" 가 아니라
  "**그 날 ScrapeRun 이 completed / partial 로 끝났느냐**" 의 proxy 다.
- "수집은 됐지만 변화 0건" 인 날도 캘린더에서 **활성** (클릭 가능).
  - 클릭 시 A 섹션은 카드 5종 모두 0 + expand 영역도 0 건. fallback 안내문은
    뜨지 않는다 (해당 날 snapshot 자체가 존재하므로 fallback 이 발동되지 않음).
- "수집 자체가 그날 한 번도 completed / partial 로 끝나지 않은 날" (전부 failed
  / cancelled / 실행 자체가 없었던 날) 은 snapshot row 가 없어 캘린더에서
  **비활성** (시각적으로 흐리게 + click disabled, 클릭 무시). 이 경우만
  비교일로 잡았을 때 §4.2 의 fallback 이 발동된다.

**modify v2 의 사용자 원문 가정 정정 표기**:

본 문서 §2.5 표를 README.USER.md 의 "대시보드 사용법" 섹션에서도 1줄로 인용
한다 — "캘린더의 활성 표시는 '변화가 있었던 날' 이 아니라 '수집이 끝까지 돌아간
날' 이다 (변화 0건이어도 활성)." 사용자가 캘린더에서 활성 날짜를 클릭했는데
A 섹션 카드가 모두 0 으로 뜨는 경우의 인지 부담을 README 한 줄로 흡수한다.

### §4.2 비교일 fallback 알고리즘

기준일은 캘린더에서 가용 날짜만 클릭 가능 → fallback 불필요 (사용자 원문 그대로).

비교일은 드롭다운 선택 (전날 / 전주 / 전월 / 전년) 또는 직접 선택 캘린더로
들어오는데, 직접 선택 캘린더는 가용 날짜만 활성이지만 드롭다운 선택은 산술
계산 결과가 가용 날짜가 아닐 수 있다. 이 경우의 처리:

```
def resolve_comparison_snapshot(session, *, requested_date) -> tuple[date | None, str | None]:
    """
    Returns (effective_snapshot_date, fallback_message) — None 이면 사용 안 됨.
    """
    if get_scrape_snapshot_by_date(session, requested_date) is not None:
        return requested_date, None
    # 직전 가용 snapshot 검색 (snapshot_date < requested_date 중 최댓값).
    nearest = find_nearest_previous_snapshot_date(session, target_date=requested_date)
    if nearest is None:
        return None, None  # A 섹션 "데이터 없음" — §4.3 의 (b) 안내문 별도 처리.
    return nearest, build_fallback_message(requested_date, nearest)
```

발동 조건은 두 가지:

(a) `requested_date` snapshot 미존재 + 직전 snapshot 존재 → A 섹션은 직전
    snapshot 으로 머지 + 안내문 (§4.3 (a)).
(b) `requested_date` 이전 snapshot 자체가 전무 → A 섹션 "데이터 없음" + B 섹션
    정상 (사용자 원문 검증 8).

**B 섹션 영향 없음**: B 섹션은 DB select 기반(`is_current=True` 활성 공고)
이라 snapshot 가용성과 무관하게 정상 표시된다.

### §4.3 안내문 문구 (사용자 원문 그대로)

(a) fallback 발동 — 직전 snapshot 사용:

```
비교일 {requested_date} 일자 snapshot 이 없어 {effective_date} 일자 snapshot
을 사용했습니다.
```

(b) 비교일 이전 snapshot 전무 — A 섹션 데이터 없음:

```
데이터 없음
```

(B 섹션 정상 표시는 그대로 유지 — A 섹션 카드 영역에만 회색 박스로 표기.)

(c) B 섹션 `to` 가 과거 (사용자 원문 그대로):

```
기준일이 과거라 표시되는 정보는 현재 기준이며 정확하지 않을 수 있습니다.
```

세 안내문 모두 응답 JSON / 페이지 컨텍스트에 effective_snapshot_date /
requested_snapshot_date 두 필드로 동봉한다 (검증용).

---

## §5. 비교 기준 계산 + (from, to] 누적 머지 알고리즘

### §5.1 (from, to) 결정 (사용자 원문 그대로)

| compare_mode | from 산출 | to 산출 |
| --- | --- | --- |
| `prev_day` | `base_date - 1일` | `base_date` |
| `prev_week` | `base_date - 7일` | `base_date` |
| `prev_month` | `base_date - relativedelta(months=1)` | `base_date` |
| `prev_year` | `base_date - relativedelta(years=1)` | `base_date` |
| `custom` | `compare_date` | `base_date` |

`relativedelta` 는 `dateutil.relativedelta` 를 사용한다 (Phase 4 의 KST 컨벤션
범위 안 — date 산술이지 datetime tz 변환이 아니라 timezone 모듈은 거치지 않는
다). `base_date` / `compare_date` 는 `datetime.date` (KST date) 다.

### §5.2 머지 reduce 의사 코드 — `merge_snapshot_payload` 재사용

`(from, to]` 구간 안의 모든 ScrapeSnapshot 을 `snapshot_date` 오름차순으로
누적 머지한다. **신규 머지 함수를 만들지 않고**
`app.db.snapshot.merge_snapshot_payload(existing, new)` 를 외부에서 두 payload
합치는 의미로 그대로 reduce 한다 (사용자 원문 주의사항 "신규 머지 함수 금지").

```python
from functools import reduce
from app.db.snapshot import merge_snapshot_payload, normalize_payload

def aggregate_payloads(snapshots: list[ScrapeSnapshot]) -> dict[str, Any]:
    """(from, to] 구간의 ScrapeSnapshot 을 시간순 누적 머지한다.

    Args:
        snapshots: snapshot_date 오름차순으로 정렬된 ScrapeSnapshot 리스트.
                   비어 있으면 정규형 빈 payload (5종 [] + counts=0) 를 반환.

    Returns:
        merge_snapshot_payload 머지 규칙(§9.4) 이 누적 적용된 정규형 payload.
        counts 는 머지된 5종 배열 길이로 자동 재계산된다.
    """
    payloads = (s.payload for s in snapshots)
    return reduce(merge_snapshot_payload, payloads, normalize_payload({}))
```

핵심:

- `reduce(merge_snapshot_payload, payloads, normalize_payload({}))` — 초깃값을
  정규형 빈 dict 로 두면 `merge_snapshot_payload` 의 normalize 단계가 또 한 번
  돌아도 idempotent 하다 (불필요한 비용은 있지만 정확성은 보장).
- N=2 일 때 (단일 비교일 vs 단일 기준일) 결과가 사용자 원문 검증 14 의 "단일
  snapshot 비교와 일관" 회귀 시나리오와 일치한다 — `merge(empty, A)` ≡ A,
  `merge(A, B)` 가 사용자 원문 머지 규칙 (`existing` / `new` 두 payload 머지)
  과 똑같이 동작.
- transition 머지의 "first from 유지 + last to 갱신 + from==to 제거" 규칙은
  `app/db/snapshot.merge_snapshot_payload` 에 이미 박혀 있어, reduce 가
  순서대로 호출하면 자연스럽게 N일치로 확장된다.

### §5.3 카운트 정합성 규칙 (사용자 원문 주의사항 그대로)

UI 카드의 카운트 숫자는 **머지된 payload 의 `counts` 합산** 으로 표시한다
(`payload.counts[CATEGORY_NEW]` 등). ID 리스트 길이를 따로 세어 표시하지
않는다 (사용자 원문 주의사항: "카운트는 머지 결과의 ID 리스트 길이가 아닌
머지된 payload 의 counts 합산"). 이 규칙은 `_build_counts` 가 머지 시점에
이미 5종 배열 길이로 재계산해 두므로 의미상 같지만, **표시 코드는 명시적으로
counts 키만 본다** — 향후 누군가 expand 영역에서 ID 리스트를 sub-filter 한
뒤 그 길이로 카운트를 출력하는 회귀를 막기 위함이다.

### §5.4 ID → 표시 메타 JOIN (N+1 회피)

머지 결과의 5종 카테고리 ID 리스트를 union 한 뒤 **한 번의 IN 쿼리** 로
`Announcement` 표시 메타를 가져온다 (Phase 1b / 3a / 3b 의 `get_X_id_set` /
`get_X_map` 헬퍼 패턴 그대로):

```python
def list_announcements_by_ids(
    session: Session, *, announcement_ids: Iterable[int]
) -> list[Announcement]:
    """주어진 announcement_id 들을 한 번의 IN 쿼리로 fetch.

    표시에 필요한 컬럼: id, source_type, status, title, agency,
    deadline_at, canonical_group_id (위젯 4번 쿼리에 활용).
    """
```

이 헬퍼는 announcement 단위 표시 메타 + canonical_project_id (정확히는
`canonical_group_id` 컬럼명, §8.4 참조) 를 반환한다. 위젯 3·4 (§8.2) 가 같은
ID 리스트를 그대로 재사용해 추가 쿼리 없이 카운트한다.

---

## §6. A 섹션 — 5종 카테고리 카드 + expand 리스트

### §6.1 카드 형식

각 카테고리(신규 / 내용 변경 / 전이 → 접수예정 / 전이 → 접수중 / 전이 → 마감)
마다 카드 1개. 카드 본문 (사용자 원문 그대로):

```
기준일 신규 N건  ↑/↓ X (비교일 M건 대비)
```

- N: 기준일 (`to`) snapshot 의 카운트 (단일 snapshot.payload.counts[*]).
- M: 비교일 (`from` 의 effective snapshot — fallback 적용 후) 의 카운트.
  비교일 snapshot 이 없으면 (§4.2 (b)) 카드 자체에 "—" 로 표기.
- X: `N - M` 의 절대값. 부호에 따라 ↑ / ↓ / "변동 없음" 선택.

기준일 카운트 N 은 `(from, to]` 누적 머지 결과의 counts 가 아니라 **단일
기준일 snapshot 의 counts** 를 표시한다 — 사용자 원문 "기준일 신규 N건" 표현
의 자연스러운 해석은 "기준일에 새로 잡힌 변화" 다. expand 리스트는 누적 머지
결과 전체를 보여준다는 점이 다르다 (§6.2 도입부 참조).

### §6.2 expand 표시 형식 (사용자 원문 그대로)

각 카드를 클릭하면 카드 아래 영역이 CSS transition 으로 펼쳐진다. 펼친 영역은
`(from, to]` 누적 머지 결과의 카테고리 ID 전체를 §5.4 헬퍼로 fetch 한 결과를
표시한다.

```
[신규 12건]
IRIS  접수예정  2025년도 인공지능 대학원 지원사업 ...   마감 2025-06-30
NTIS  접수중    양자컴퓨팅 기초연구 신규과제 공고 ...   마감 2025-05-15
[내용 변경 8건]
IRIS  접수중    제조혁신 R&D 사업 (마감일 변경됨)      마감 2025-05-20  [📝 내용 변경] [🔄 전이→접수중도]
[전이 → 접수중 5건]
IRIS  접수중    스마트팜 기술개발 사업 (접수예정에서)   마감 2025-06-15
```

행 구성:

- 소스 배지 (IRIS / NTIS) — 1단어.
- 현재 status (접수중 / 접수예정 / 마감) — Announcement.status 한글값.
- 제목 — 클릭 시 `/announcements/{id}` 로 이동 (`<a href>` 표준 동작 — 가운데
  클릭 시 새 창, 사용자 원문 검증 10).
- 내용 변경 행은 동일 announcement 가 다른 카테고리에도 등장한 경우 작은 배지
  추가: `📝 내용 변경` / `🔄 전이→접수중도` 등 (사용자 원문 형식 그대로).
- 전이 행은 from 표기를 같이 노출: `(접수예정에서)` 형태 (사용자 원문 그대로).
- 마감일은 KST 일자 — Jinja2 `kst_date` 필터 경유: `{{ ann.deadline_at | kst_date }}`.

각 행 전체가 클릭 영역 (`<a href>`). expand 영역 안에 다른 인라인 컨트롤이
들어갈 일은 없으므로 (대시보드 read-only) 사용자 원문 주의사항 "expand 영역
row 클릭과 다른 인라인 컨트롤 충돌 주의 (Phase 3a `event.stopPropagation()`
패턴)" 는 본 task 에서는 추가 컨트롤이 들어오는 시점에 한 번 더 확인할 사항
으로 둔다 (현재 범위 안에서는 충돌 없음 — `<a href>` 단일 click target).

### §6.3 카드 / expand 데이터 흐름

```
GET /dashboard?base_date=...&compare_mode=...&compare_date=...
  ├─ snapshot 조회: get_scrape_snapshot_by_date(session, base_date)         (단일 row)
  ├─ snapshot 조회: get_scrape_snapshot_by_date(session, compare_date)      (단일 row)
  │   └─ None 이면 §4.2 의 nearest fallback
  ├─ 누적 머지 대상: list_snapshots_in_range(session, from_exclusive=from_date, to_inclusive=to_date)
  │   └─ payload list 를 reduce(merge_snapshot_payload, ..., empty) (§5.2)
  ├─ 5종 카테고리 ID union → list_announcements_by_ids(session, ids)       (§5.4, IN 쿼리 1회)
  └─ 템플릿에 dict 로 전달:
       base_payload, compare_payload, merged_payload, ann_meta_map,
       fallback_message, base_snapshot_date, compare_requested_date,
       compare_effective_date.
```

---

## §7. B 섹션 — to 기준 1개월 이내 활성 공고

### §7.1 쿼리 (사용자 원문 그대로)

```python
def list_soon_to_open_announcements(
    session: Session, *, to_kst_date: date, days: int = 30
) -> list[Announcement]:
    """is_current=True AND status='접수예정' AND received_at BETWEEN to AND to+days."""

def list_soon_to_close_announcements(
    session: Session, *, to_kst_date: date, days: int = 30
) -> list[Announcement]:
    """is_current=True AND status='접수중' AND deadline_at BETWEEN to AND to+days."""
```

세부:

- 둘 다 `is_current=True` 활성 공고만 대상 — 사용자 원문 주의사항 "to 가
  과거인 B 섹션은 `is_current=True` 만 검색 (이력 row 활용은 범위 밖)".
- BETWEEN 경계의 KST→UTC 변환은 `kst_date_boundaries(to_kst_date)` (시작) +
  `kst_date_boundaries(to_kst_date + timedelta(days=days))` (끝) 로 만든
  `[start_utc, end_utc)` 반-open 구간을 그대로 사용한다 (Phase 4 컨벤션).
- 정렬: 접수예정은 `received_at ASC`, 접수중은 `deadline_at ASC` (각각 임박
  순). limit 은 두지 않는다 — 30일 이내 active 만이라 보통 충분히 작다.
- 표시 컬럼: 제목 (상세 링크) + 소스 배지 + 마감일 (KST, `kst_date` 필터).

### §7.2 to 가 과거인 경우

`to_kst_date < now_kst().date()` 이면 회색 박스로 §4.3 (c) 안내문 표기 후
정상 표시. 사용자 원문 검증 11 의 "안내문 + 현재 시점 활성 공고 표시" 그대로 —
이력 row (`is_current=False`) 활용은 본 task 범위 밖.

---

## §8. 사용자 라벨링 위젯 (로그인 시) + 신규 헬퍼 4종

### §8.1 4종 위젯 구조

| # | 위젯 라벨 | 단위 | 범위 |
| --- | --- | --- | --- |
| 1 | 전체 미확인 공고: N건 | announcement (읽음) | 날짜 무관 |
| 2 | 전체 미판정 관련성: M건 | canonical (관련성) | 날짜 무관 |
| 3 | 기준일 변경 공고 중 내 미확인: K건 | announcement (읽음) | (from, to] 머지된 announcement_ids 에 한정 |
| 4 | 기준일 변경 공고 중 내 미판정: L건 | canonical (관련성) | (from, to] 머지된 ID 의 canonical_project_id 에 한정 |

위젯 3 의 `announcement_ids` 와 위젯 4 의 `canonical_ids` 는 §5.4 의
`list_announcements_by_ids` 결과를 그대로 재사용한다 — A 섹션이 이미 fetch
한 announcement row 의 `id` / `canonical_group_id` 를 모은 것 (추가 쿼리
없음).

### §8.2 신규 헬퍼 함수 시그니처 (PROJECT_NOTES "N+1 제거 패턴")

`app/db/repository.py` 에 추가 (구현은 00042-5 subtask). 모든 헬퍼는 단일 IN
쿼리 또는 단일 COUNT 쿼리로 끝낸다.

```python
def count_unread_announcements_for_user(
    session: Session, *, user_id: int
) -> int:
    """(전체) 미확인 공고 수.

    조건:
      - is_current=True 활성 announcements 만.
      - NOT EXISTS (
            SELECT 1 FROM announcement_user_states
            WHERE announcement_id = announcements.id
              AND user_id = :user_id AND is_read = TRUE
        )
        — 즉 사용자가 한 번도 읽지 않았거나 읽었지만 내용 변경으로 is_read=False
        리셋된 공고 (Phase 1b 정책 그대로).
    """


def count_unjudged_canonical_for_user(
    session: Session, *, user_id: int
) -> int:
    """(전체) 미판정 canonical 수.

    조건:
      - canonical_projects 전체에 대해
        NOT EXISTS (
            SELECT 1 FROM relevance_judgments
            WHERE canonical_project_id = canonical_projects.id
              AND user_id = :user_id
        ).
      - 사용자 원문: \"canonical 단위(관련성) vs announcement 단위(읽음) 혼용
        금지\" — relevance_judgments 만 보고 announcements 는 보지 않는다.
    """


def count_unread_in_announcement_ids(
    session: Session, *, user_id: int, announcement_ids: Iterable[int]
) -> int:
    """주어진 announcement_ids 중 사용자가 미확인인 개수 (단일 IN 쿼리).

    빈 리스트면 쿼리 없이 0 을 반환 (회귀 가드).
    """


def count_unjudged_in_canonical_ids(
    session: Session, *, user_id: int, canonical_ids: Iterable[int]
) -> int:
    """주어진 canonical_ids 중 사용자가 관련성 미판정인 개수 (단일 IN 쿼리).

    빈 리스트면 쿼리 없이 0 을 반환 (회귀 가드).
    """
```

snapshot 조회 헬퍼도 함께 추가 (구현은 00042-2 subtask):

```python
def list_available_snapshot_dates(session: Session) -> list[date]:
    """scrape_snapshots 의 snapshot_date 전체 (오름차순). 캘린더 + API 응답."""


def find_nearest_previous_snapshot_date(
    session: Session, *, target_date: date
) -> date | None:
    """target_date 이전 (배타) 의 가장 가까운 snapshot_date. 없으면 None.

    SELECT MAX(snapshot_date) FROM scrape_snapshots
    WHERE snapshot_date < :target_date.
    """


def list_snapshots_in_range(
    session: Session, *, from_exclusive: date, to_inclusive: date
) -> list[ScrapeSnapshot]:
    """(from, to] KST 날짜 구간의 ScrapeSnapshot list (snapshot_date ASC).

    \"(from, to]\" 의 \"(\" 는 사용자 원문 그대로 from 을 비교일로 잡되
    그 날의 변화는 비교 대상 (already-baseline) 에 포함시키지 않으려는 의도다.
    SQL: WHERE snapshot_date > :from_exclusive AND snapshot_date <= :to_inclusive.
    """


def list_announcements_by_ids(
    session: Session, *, announcement_ids: Iterable[int]
) -> list[Announcement]:
    """A 섹션 expand + 위젯 3·4 가 공유하는 단일 IN 쿼리 헬퍼 (§5.4)."""
```

### §8.3 비로그인 시 처리 (사용자 원문 주의사항)

```python
@router.get("/dashboard")
def dashboard_page(
    request: Request,
    base_date: date | None = Query(default=None),
    compare_mode: str = Query(default="prev_day"),
    compare_date: date | None = Query(default=None),
    session: Session = Depends(get_session),
    current_user: User | None = Depends(current_user_optional),
):
    ...
    if current_user is not None:
        # 위젯 4 종 카운트 — 4번의 단일 쿼리 (총 4 query). N+1 아님.
        widgets = build_user_label_widgets(
            session,
            user_id=current_user.id,
            announcement_ids=merged_announcement_ids,
            canonical_ids=merged_canonical_ids,
        )
    else:
        widgets = None  # 템플릿에서 {% if widgets %} 로 영역 자체 skip.
        logger.debug("dashboard: 비로그인 — 위젯 쿼리 skip")
```

- 비로그인 시 **헬퍼 4종 자체를 호출하지 않는다** — DEBUG 로그로 검증 16 의
  "비로그인 시 위젯 쿼리 자체 skip" 확인.
- 템플릿에서도 `{% if widgets %} … {% endif %}` 로 영역을 통째로 skip — DOM
  에 위젯 영역 div 가 들어가지 않는다.

### §8.4 canonical(관련성) vs announcement(읽음) 단위 구분

PROJECT_NOTES 결정사항 — Phase 1b 부터 일관되게 적용된 컨벤션:

- **읽음**: `announcement_user_states.announcement_id` 단위. IRIS 공고를 읽었다
  고 동일 canonical 의 NTIS 공고가 자동 읽음 처리되지 않는다.
- **관련성**: `relevance_judgments.canonical_project_id` 단위. canonical 그룹
  단위로 관련 / 무관 판정.
- 위젯 1 / 3 = 읽음 = announcement 단위. 위젯 2 / 4 = 관련성 = canonical 단위.

스키마 컬럼명 주의 (실제 코드 검증):

- `Announcement.canonical_group_id` (FK to `canonical_projects.id`)
- `RelevanceJudgment.canonical_project_id` (FK to `canonical_projects.id`)

두 컬럼 이름이 다르지만 가리키는 PK 는 같다 (`canonical_projects.id`). 위젯 4
의 입력은 `Announcement.canonical_group_id` 의 None 이 아닌 set 을
`RelevanceJudgment.canonical_project_id` IN 절에 넣는 형태로 작동한다.

---

## §9. 추이 차트 (Chart.js, 기준일 ±15일)

### §9.1 데이터 구성

`base_date` 를 가운데 두고 `base_date - 15일` 부터 `base_date + 15일` 까지 31일
배열을 만든다 (정확히 ±15 = 30 일이 아닌 31 일 — 양 끝점 포함). 사용자 원문
"기준일 중심 ±15일 (총 30일)" 의 자연스러운 해석상 31일 / 30일 둘 다 가능하나
구현은 **31일 (양끝 포함)** 로 명시한다.

각 날짜에 대해:

```python
{
    "date": "2026-04-15",                  # 그래프 x 라벨 (KST date ISO)
    "label": "04-15",                      # format_kst(date, "%m-%d") 동치
    "new": 12,                             # snapshot.payload.counts[CATEGORY_NEW]
    "content_changed": 8,                  # counts[CATEGORY_CONTENT_CHANGED]
    "transitioned": 5,                     # counts[transitioned_to_*] 3종 합계
}
```

snapshot 이 없는 날짜는 **0 으로 채운다** (gap 처리는 하지 않음 — Chart.js
의 line chart 에서 0 카운트가 자연스럽게 골짜기로 표시되어 직관적). 사용자
원문 "snapshot 없는 날짜는 0 또는 gap 처리" 둘 다 허용 → 0 채움 채택.

### §9.2 서버 사전 계산 + JSON 임베드

사용자 원문 주의사항: "Chart.js 데이터는 서버에서 사전 계산 후 JSON 으로 페이지
임베드 (별도 API 호출 X)".

```html
{# 템플릿 안에서 #}
<canvas id="dashboardTrendChart" width="800" height="200"></canvas>
<script id="dashboardTrendData" type="application/json">
  {{ trend_data | tojson | safe }}
</script>
<script src="/static/vendor/chart.min.js"></script>
<script src="/static/js/dashboard.js"></script>
```

`dashboard.js` 가 `#dashboardTrendData` 의 textContent 를 `JSON.parse` 해서
Chart.js 인스턴스를 만든다. 별도 `GET /dashboard/api/trend` 같은 API 는
만들지 않는다.

### §9.3 Chart.js 번들링 + 라이선스

- 번들 위치: `app/web/static/vendor/chart.min.js`. 외부 CDN 의존 없음 — 사용자
  원문 컨벤션 ("외부 CDN 의존 없음").
- 버전: Chart.js v4 LTS 의 stable 최신. 다운로드 URL 과 SHA256 (또는 SHA512) 을
  본 문서 §9.3 마지막에 박는다 — 향후 재현 가능하게.
- 라이선스: MIT. 프로젝트 루트에 `NOTICE` 신설하고 다음 항목 추가:

  ```
  third_party/chart.min.js — Chart.js, MIT License
  Copyright (c) 2014-... Chart.js Contributors
  https://github.com/chartjs/Chart.js/blob/master/LICENSE.md
  ```

  `LICENSE` 파일은 본 task 에서 새로 만들지 않는다 (프로젝트 자체의 라이선스
  결정은 본 task 범위 밖). `NOTICE` 만 만들고 third-party 항목을 모은다.

- 캘린더 (자체 구현, §10) 는 추가 라이선스 항목 불필요.

---

## §10. 캘린더 라이브러리 선택

### §10.1 후보 비교

(a) **순수 CSS + JS 자체 구현** — 월별 그리드, 가용 날짜 배경 강조 / click
    disabled.
- 장점: 외부 의존 0. 코드 ~150 줄 수준 (월 navigation + 날짜 그리드 + 가용
  날짜 set lookup). 디자인 / disabled-state 풀 컨트롤. 라이선스 부담 0.
  Phase 3a / 3b 의 자체 모달 / 자체 트리 컴포넌트와 톤 일치.
- 단점: 키보드 네비게이션 / 접근성 / i18n 등을 직접 구현해야 함. 본 task 범위
  에서는 키보드 네비게이션 / i18n 요구 없음 (KST 단일).

(b) **Pikaday 같은 작은 라이브러리** — `pikaday.js` (~30KB) 번들링.
- 장점: 키보드 네비게이션 / focus management 가 검증되어 있음.
- 단점: 외부 의존 1 추가 (BSD 라이선스 — `NOTICE` 항목 추가 필요). 가용 날짜
  강조 / disabled 는 `disableDayFn` 콜백을 자체 작성해야 해 결국 약간의
  자체 코드는 필요. Pikaday 자체는 메인테너 활동이 거의 멈춘 상태 (마지막
  release 2018) — 수년 후 보안 / 모던 브라우저 호환 부담을 본 프로젝트가
  떠안게 됨.

### §10.2 결정 — (a) 순수 CSS + JS 자체 구현

본 task 의 캘린더 요구 (월별 그리드 + 가용 날짜 강조 + 비가용 click disabled)
범위가 좁고, Phase 3b 의 `_favorites_modal.html` / `favorites_page.js` 톤과
일치시키려는 의도, 외부 의존 추가 회피 컨벤션을 종합해 **자체 구현** 으로
결정한다.

구현 위치 (00042-2 subtask):

- `app/web/static/js/dashboard_calendar.js` — Calendar 컴포넌트 (월 grid 렌더
  + 좌우 화살표 + 가용 set lookup + disabled click 무시).
- `app/web/static/css/style.css` 에 캘린더 클래스 추가 (`.dashboard-calendar`
  / `.dashboard-calendar__day--available` / `.dashboard-calendar__day--disabled`).

캘린더 컴포넌트의 입력은 `(GET /dashboard/api/snapshot-dates 의 응답)` JSON
배열을 페이지 초기 로딩 시 한 번 fetch 해 set 으로 만든다. 같은 set 을 기준일
캘린더 / 비교일 캘린더 두 인스턴스가 공유한다.

### §10.3 가용 날짜 시각화 규칙 (사용자 원문 그대로)

- 가용 (snapshot row 존재 — §4.1): 색 배경 + 동그라미 등 시각 강조.
- 비가용: 흐리게 (opacity 0.4 정도) + click disabled. JS 에서 `event.preventDefault()`
  + 클래스가 활성이 아닐 시 onclick 리스너 자체에서 early-return.

---

## §11. Endpoint 설계

| Method | Path | 인증 | 응답 | 비고 |
| --- | --- | --- | --- | --- |
| GET | `/dashboard` | 비로그인 가능 | HTML | query: `base_date`, `compare_mode`, `compare_date`. 모두 optional. |
| GET | `/dashboard/api/snapshot-dates` | 비로그인 가능 | JSON `{"dates": ["2026-04-15", ...]}` | 캘린더 컴포넌트 초기 로딩용. |

### §11.1 GET /dashboard 쿼리 파라미터 정규화

- `base_date` (KST date ISO `YYYY-MM-DD`). 미지정 / 빈문자열 / 파싱 실패 →
  `now_kst().date()`. 가용 날짜가 아니어도 페이지는 뜨고 (캘린더에 그날을
  default selected 로 표시), A 섹션은 §4.2 와 같은 fallback 로직을 기준일에도
  방어적으로 한 번 적용한다 (`get_scrape_snapshot_by_date` 가 None 이면
  `find_nearest_previous_snapshot_date` 로 안내문 표시) — 외부에서 직접 URL 을
  치는 경우의 회귀 방어.
- `compare_mode` ∈ {`prev_day`, `prev_week`, `prev_month`, `prev_year`,
  `custom`}. 다른 값이면 `prev_day` 로 fallback (UI 가 5종만 노출하므로 보통
  발생하지 않음).
- `compare_date` (KST date ISO). `compare_mode == "custom"` 일 때만 의미를
  가진다. 그 외 mode 에서는 무시.

### §11.2 GET /dashboard/api/snapshot-dates 응답

```json
{
    "dates": ["2026-04-15", "2026-04-16", "2026-04-22", ...]
}
```

`list_available_snapshot_dates(session)` 결과를 그대로 ISO 문자열 list 로
직렬화. 정렬은 오름차순. 빈 set 은 `{"dates": []}`.

### §11.3 기존 라우트는 그대로 활용 (POST 없음)

- 공고 상세: `/announcements/{id}` — A 섹션 expand 행 클릭 시 이동.
- 즐겨찾기 / 관련성 / 읽음 처리 — 대시보드는 read-only 라 POST 엔드포인트를
  추가하지 않는다.

---

## §12. 페이지 새로고침 정책 (사용자 원문 그대로)

- 자동 갱신 안 함. 수집이 새로 끝나도 사용자가 reload 해야 반영.
- 컨트롤 변경 (기준일 / 비교 대상) 은 변경 즉시 페이지 reload (form GET
  submission) — fragment 갱신 (XHR partial render) 은 본 task 범위 밖.
- 캘린더에서 가용 날짜를 클릭하면 `base_date` / `compare_date` 쿼리 파라미터
  를 갱신해 `window.location.assign(url)` — 페이지 전체 reload.

---

## §13. 검증 시나리오 (사용자 원문 17 항목 그대로)

테스트는 `tests/dashboard/test_dashboard_routes.py` (00042-7 subtask) 가
fixture 로 ScrapeSnapshot 을 직접 INSERT 한 뒤 라우트를 호출해 응답 본문 / DEBUG
로그 / 쿼리 카운트를 검증한다.

| # | 시나리오 | 기대 결과 |
| --- | --- | --- |
| 1 | 비로그인 / dashboard 접근 | 페이지 로드, 라벨링 위젯 영역 미표시 |
| 2 | 로그인 후 / dashboard | 라벨링 위젯 4종 표시 |
| 3 | 기준일 = 오늘, 비교 = 전날 | A / B 섹션 정상 |
| 4 | 기준일 = 어제 (snapshot 있음), 비교 = 전주 | 정상 |
| 5 | 기준일 캘린더의 snapshot 없는 날짜 | 클릭 비활성 |
| 6 | 비교 = 직접 선택 + 가용 날짜 | 정상 |
| 7 | 비교 = 직접 선택 + 가용 안 됨 | 가장 가까운 이전 snapshot 사용 + 안내문 |
| 8 | 비교일 이전 snapshot 전무 | A 섹션 \"데이터 없음\", B 섹션 정상 |
| 9 | A 섹션 카드 클릭 | expand, 전체 리스트 |
| 10 | row 클릭 / 가운데 클릭 | 상세 페이지 이동 / 새 창 (`<a href>` 표준) |
| 11 | B 섹션 to=과거 | 안내문 + 현재 시점 활성 공고 표시 |
| 12 | 추이 차트 | 기준일 ±15일 일별 카운트 line chart |
| 13 | 모든 timestamp KST | Jinja2 필터 경유 확인 |
| 14 | (from, to] 누적 머지 회귀 | 단일 snapshot 비교와 일관, `merge_snapshot_payload` 재사용 |
| 15 | 위젯 쿼리 N+1 회피 | announcement_ids 한 번의 IN 쿼리 |
| 16 | 비로그인 시 위젯 쿼리 skip | DEBUG 로그로 확인 |
| 17 | 5a 회귀 | snapshot 생성 / 머지 / GC 동작 영향 없음 (대시보드 read-only) |

추가 (modify v2) 회귀:

| # | 시나리오 | 기대 결과 |
| --- | --- | --- |
| 18 | 변화 0건 ScrapeRun completed 종료 후 dashboard 접근 | 해당 날짜 캘린더에서 활성 (snapshot row 존재). A 섹션 카드 5종 모두 0 + expand 0건. fallback 안내문 미표시. |
| 19 | 그 날 ScrapeRun 이 모두 failed / cancelled | 캘린더에서 비활성. 비교일로 잡았을 때 §4.2 fallback 발동. |

---

## §14. 후속 subtask 분담 매트릭스

| subtask | 산출물 | 본 문서 § |
| --- | --- | --- |
| 00042-2 | GET /dashboard 라우트 골격 + (from,to) 산출 함수 + GET /dashboard/api/snapshot-dates + 캘린더 컴포넌트 (자체 구현) + 네비 \"대시보드\" 탭 | §3 / §5.1 / §10 / §11 |
| 00042-3 | A 섹션 — `merge_snapshot_payload` reduce 누적 + 5종 카드 + expand 리스트 + IN 쿼리 헬퍼 (`list_announcements_by_ids`) + fallback 안내문 | §4 / §5.2–§5.4 / §6 |
| 00042-4 | B 섹션 — DB select (`list_soon_to_open` / `list_soon_to_close`) + to 과거 안내문 | §7 |
| 00042-5 | 사용자 라벨링 위젯 4종 (로그인 시) + 신규 헬퍼 4종 (`count_unread_*` / `count_unjudged_*`) + 비로그인 skip + DEBUG 로그 | §8 |
| 00042-6 | 추이 차트 — Chart.js 로컬 vendor 번들 + 31일 일별 카운트 사전 계산 + JSON 임베드 + NOTICE 라이선스 항목 | §9 |
| 00042-7 | tests/dashboard/test_dashboard_routes.py (검증 1–19) + README.USER.md \"대시보드 사용법\" 섹션 + 5a 회귀 영향 없음 확인 + PROJECT_NOTES MemoryUpdater | §13 |

각 subtask 는 본 문서 § 번호를 PR 설명 / 코드 주석에 인용해 합의 근거를
명시한다 — 본 문서가 살아 있는 동안 § 번호는 변경하지 않는다 (§ 도입부 약속).
