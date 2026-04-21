# 공고 상태 전이 처리 TODO

이 문서는 증분 수집 전략에서 아직 실제로 검증되지 않은 **상태 전이 경로**와
향후 구현 시 참고해야 할 사항을 정리한다.

## 현재 상태

- **수집 범위**: IRIS 접수예정·접수중·마감 3개 상태 전체 수집 (순차 루프, `ancmPrg` 파라미터).
- **발생 가능 여부**: 3개 상태 수집 범위에서 동일 공고가 다른 상태로 재등장하면 발생한다.
- **구현 상태**: `status_transitioned` 분기 정상 운영 경로로 전환됨 (in-place UPDATE, INFO 로그).

## 상태 전이 분기

`app/db/repository.upsert_announcement` 의 4-branch 중 (c) 분기:

```
기존 row 존재 + changed_fields == {"status"} 만 변경
  → 기존 row in-place UPDATE (status 갱신)
  → action="status_transitioned", needs_detail_scraping=True
```

`app/cli._log_upsert_action` 에서 `action="status_transitioned"` 일 때 **INFO** 로그를 남긴다.

## 발동 조건

동일 공고(`source_type` + `source_announcement_id`)가 다른 상태로 재등장하는 경우:

| 시나리오 | 예시 |
|---|---|
| 접수예정 → 접수중 | 다음 수집 시 접수중 목록에 동일 공고 ID 등장 |
| 접수중 → 마감 | 마감 목록을 수집할 때 동일 ID 재등장 |
| 접수중 → 접수예정 | 거의 없지만 공고 정정 등으로 발생 가능 |

## IRIS 접수예정·마감 수집 — 구현 완료 항목

[00012]에서 구현 완료:

1. [완료] `list_scraper.scrape_list` 에 상태 필터 파라미터 추가 — `ancmPrg=ancmPre|ancmIng|ancmEnd` 3개 상태 순차 루프
2. [완료] IRIS API 응답의 상태 문자열 → `AnnouncementStatus` Enum 매핑 검증 — `_map_api_record_to_dict`에서 `status_label` 직접 주입
3. `status_transitioned` 경로가 실제로 발생하는지 소량 데이터로 먼저 검증
4. CLI 에서 상태 전이 감지 시 추가 처리(알림, 별도 로그 등) 필요 여부 판단

검증 방법 예시:
```bash
# 소수 페이지만 수집 후 INFO 로그 확인
python -m app.cli run --max-pages 2 --skip-detail --log-level DEBUG
```

## NTIS 등 신규 크롤러 구현 시

`app/scraper/ntis/adapter.py` 상단 TODO 참조.

- `scrape_list` 반환 row 에 `status` 키 필수 포함
- `status` 값은 반드시 `AnnouncementStatus` Enum 값 중 하나
  (`"접수중"` / `"접수예정"` / `"마감"`)
- 증분 수집 4-branch 는 repository 계층이 자동 처리하므로 별도 구현 불필요

## 이력 보존 구조 (참고)

`action="new_version"` 분기: title / deadline_at / agency 등 핵심 내용이 변경된 경우,
기존 row 를 `is_current=False` 로 봉인하고 신규 row 를 INSERT 한다.

- 이력 조회: `SELECT * FROM announcements WHERE source_announcement_id=? AND is_current=0`
- 현재 버전 조회: `SELECT * FROM announcements WHERE source_announcement_id=? AND is_current=1`

첨부파일(`attachments` 테이블)은 구버전 row(is_current=False)에 연결된 채 보존된다.
신규 버전 row 에 첨부파일을 재연결하는 로직은 아직 구현되지 않았다.
(첨부파일 다운로드 기능 활성화 시 함께 검토 권장)
