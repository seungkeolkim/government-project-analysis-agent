import { test, expect, Page } from "@playwright/test";

// Phase A-3 / task 00125-9 — 「메일 발송」 탭의 Daily Report 카드 + 발송 이력
// 섹션 E2E 시나리오.
//
// 검증 범위 (subtask 00125-9 acceptance_criteria):
//   - Daily Report 카드가 노출되고 와이어프레임 항목(활성화 체크박스 / cron
//     입력 / 다음 실행·마지막 발송 표시 / 수신자 요약 / 테스트 발송 / 지금
//     발송 / 저장)이 모두 렌더된다.
//   - cron 표현식을 입력하고 저장하면 success flash 가 표시된다.
//   - 「Daily Report 발송 이력」 섹션이 5종 상태/3종 트리거 라벨을 처리하는
//     테이블(또는 빈 상태)을 렌더한다.
//   - 발송 이력 행을 클릭하면 수신자별 결과 expand 가 동작한다.
//
// 실제 메일 도착은 사용자 수동 검증 영역이라 본 스펙은 발송 자체를 트리거하지
// 않는다 (작업지시서 §"사용자 수동 검증 순서"). 테스트 발송/지금 발송 버튼은
// 존재·활성 여부만 확인한다.
//
// 관리자 페이지는 admin 로그인이 필요하다. 자격증명은 환경변수로 주입하며,
// 미주입 시 개발 환경 기본값(admin / admin)을 사용한다.

const BASE_URL = process.env.E2E_BASE_URL || "http://localhost:8000";
const ADMIN_USERNAME = process.env.E2E_ADMIN_USERNAME || "admin";
const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD || "admin";

const EMAIL_PAGE_URL = `${BASE_URL}/admin/email`;

/**
 * admin 계정으로 로그인한다. GET /login 폼을 채워 POST /auth/login 으로
 * 제출하면 세션 쿠키가 설정되고 / 로 리다이렉트된다.
 */
async function loginAsAdmin(page: Page): Promise<void> {
  await page.goto(`${BASE_URL}/login`);
  await page.fill("input[name=username]", ADMIN_USERNAME);
  await page.fill("input[name=password]", ADMIN_PASSWORD);
  await page.click("button[type=submit]");
  await page.waitForLoadState("networkidle");
}

test.describe("Daily Report 카드 (Phase A-3)", () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(EMAIL_PAGE_URL);
    await page.waitForLoadState("networkidle");
  });

  test("Daily Report 카드의 와이어프레임 항목이 모두 노출된다", async ({
    page,
  }) => {
    // 카드 제목.
    await expect(
      page.locator(".admin-section__heading", { hasText: "Daily Report" }).first()
    ).toBeVisible();

    // 활성화 체크박스.
    await expect(page.locator("#daily-report-enabled")).toBeVisible();

    // cron 입력 + 다음 실행 / 마지막 발송 표시.
    await expect(page.locator("#daily-report-cron")).toBeVisible();
    await expect(page.locator("#daily-report-next-run")).toBeVisible();
    await expect(page.locator("#daily-report-last-sent")).toBeVisible();

    // 수신자 요약 + 자세히 보기 expand.
    await expect(page.locator("#daily-report-recipients-summary")).toBeVisible();
    await expect(
      page.locator("details.daily-report-recipients > summary")
    ).toBeVisible();

    // 테스트 발송 / 지금 발송 / 저장 버튼.
    await expect(page.locator("#daily-report-test-recipient")).toBeVisible();
    await expect(page.locator("#daily-report-test-send-button")).toBeVisible();
    await expect(page.locator("#daily-report-send-now-button")).toBeVisible();
    await expect(page.locator("#daily-report-settings-save")).toBeVisible();
  });

  test("카드 진입 시 GET /daily-report/settings 응답으로 채워진다", async ({
    page,
  }) => {
    // 초기 로드가 끝나면 다음 실행/마지막 발송 span 이 '—' 이상의 텍스트로 채워진다.
    await expect(page.locator("#daily-report-next-run")).not.toHaveText("");
    await expect(page.locator("#daily-report-last-sent")).not.toHaveText("");

    // 수신자 요약 줄이 'admin 사용자 N명' 형식으로 렌더된다.
    await expect(
      page.locator("#daily-report-recipients-summary")
    ).toContainText("admin 사용자");
  });

  test("수신자 admin 목록 자세히 보기 expand 가 동작한다", async ({ page }) => {
    const detail = page.locator("details.daily-report-recipients");
    const detailBody = page.locator("#daily-report-recipients-detail");

    // 펼치기 전에는 details 가 닫혀 있다.
    await expect(detail).not.toHaveAttribute("open", /.*/);

    await page.locator("details.daily-report-recipients > summary").click();

    // 펼친 뒤 admin 목록 영역이 표시된다 (테이블 또는 'admin 사용자가 없습니다').
    await expect(detail).toHaveAttribute("open", "");
    await expect(detailBody).toBeVisible();
  });

  test("cron 표현식을 입력하고 저장하면 success flash 가 표시된다", async ({
    page,
  }) => {
    // 활성화는 끈 채로 cron 만 저장한다 — 실제 예약 잡을 등록하지 않아 안전하다.
    await page.locator("#daily-report-enabled").uncheck();
    await page.fill("#daily-report-cron", "0 9 * * 1-5");

    await page.click("#daily-report-settings-save");

    // 저장 성공 flash 가 카드 flash 영역에 나타난다.
    await expect(
      page.locator("#daily-report-flash-area .admin-flash--success")
    ).toBeVisible();
  });
});

test.describe("Daily Report 발송 이력 섹션 (Phase A-3)", () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(EMAIL_PAGE_URL);
    await page.waitForLoadState("networkidle");
  });

  test("발송 이력 섹션이 렌더되고 GET /daily-report/runs 결과를 표시한다", async ({
    page,
  }) => {
    await expect(
      page.locator(".admin-section__heading", {
        hasText: "Daily Report 발송 이력",
      })
    ).toBeVisible();

    await expect(
      page.locator("#daily-report-runs-refresh-button")
    ).toBeVisible();

    // fetch 가 끝나면 테이블 영역에 테이블 또는 빈 상태 문구가 채워진다.
    const tableArea = page.locator("#daily-report-runs-table-area");
    await expect(tableArea).not.toHaveText("");
  });

  test("발송 이력이 있으면 행 클릭 시 수신자별 결과가 expand 된다", async ({
    page,
  }) => {
    const firstRow = page
      .locator("#daily-report-runs-table-area .forward-history__row")
      .first();

    // 발송 이력이 한 건도 없는 환경에서는 expand 검증을 건너뛴다 (빈 상태).
    const rowCount = await page
      .locator("#daily-report-runs-table-area .forward-history__row")
      .count();
    test.skip(rowCount === 0, "Daily Report 발송 이력이 없어 expand 검증 생략");

    const expandRow = page
      .locator("#daily-report-runs-table-area .forward-history__expand-row")
      .first();

    // 클릭 전에는 expand 행이 hidden.
    await expect(expandRow).toBeHidden();

    await firstRow.click();

    // 클릭 후 expand 행이 보이고 수신자별 결과 영역이 채워진다.
    await expect(expandRow).toBeVisible();
    await expect(
      expandRow.locator(".forward-history__expand-cell")
    ).not.toHaveText("");
  });
});
