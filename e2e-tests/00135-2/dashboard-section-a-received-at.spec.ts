import { test, expect } from "@playwright/test";

// task 00135-2 — 대시보드 "A. 공고 변화 목록" expand 행에 접수 일시를 마감
// 일시 왼쪽에 함께 표시하는 변경에 대한 E2E 시나리오.
//
// 검증 범위 (subtask 00135-2 acceptance_criteria):
//   - A 섹션 카드를 expand 하면 각 행에 접수 일시가 마감 일시 "왼쪽"에 함께
//     렌더된다 (.dashboard-card__received 가 .dashboard-card__deadline 보다
//     DOM 순서상 앞).
//   - 접수 일시 데이터가 없는 행은 "접수일 미정" 으로 자연스럽게 처리된다.
//   - 00135-1 의 전이 문구 수정 결과(🔄 전이→{상태}에도)가 회귀 없이 함께
//     정상 표시되고, 과거의 깨진 "전이→{상태}도마감" 표현이 보이지 않는다.
//
// 서비스는 8000 포트에서 동작한다 (00043-1 의 대시보드 진입 패턴 그대로).

const BASE_URL = process.env.E2E_BASE_URL || "http://localhost:8000";
const DASHBOARD_URL = `${BASE_URL}/dashboard`;

test.describe("대시보드 A 섹션 — 접수·마감 일시 표시 (task 00135-2)", () => {
  test("A 섹션 카드를 expand 하면 접수 일시가 마감 일시 왼쪽에 렌더된다", async ({
    page,
  }) => {
    await page.goto(DASHBOARD_URL);
    await page.waitForLoadState("networkidle");

    const sectionA = page.locator(".dashboard-section--a");
    await expect(sectionA).toBeVisible();

    // A 섹션의 모든 카드(<details>)를 펼친다.
    const cards = sectionA.locator("details.dashboard-card");
    const cardCount = await cards.count();
    for (let index = 0; index < cardCount; index += 1) {
      await cards.nth(index).evaluate((element) => {
        (element as HTMLDetailsElement).open = true;
      });
    }

    // 펼친 카드들 안의 expand 행을 수집한다.
    const rows = sectionA.locator(".dashboard-card__expand-row");
    const rowCount = await rows.count();
    // 데이터가 있어야 의미 있는 검증이 가능하다. 시드 데이터가 비어 있으면
    // 이 테스트는 환경 문제이므로 명시적으로 실패시킨다.
    expect(
      rowCount,
      "A 섹션 expand 행이 1개 이상 있어야 접수/마감 표시를 검증할 수 있음"
    ).toBeGreaterThan(0);

    for (let index = 0; index < rowCount; index += 1) {
      const row = rows.nth(index);

      // 접수·마감 일시 묶음 컨테이너가 존재한다.
      const dates = row.locator(".dashboard-card__dates");
      await expect(dates).toHaveCount(1);

      // 접수 span / 마감 span 이 각각 1개씩 렌더된다.
      const received = dates.locator(".dashboard-card__received");
      const deadline = dates.locator(".dashboard-card__deadline");
      await expect(received).toHaveCount(1);
      await expect(deadline).toHaveCount(1);

      // 접수 span 텍스트는 "접수 ..." 또는 "접수일 미정" 형식.
      const receivedText = (await received.innerText()).trim();
      expect(receivedText).toMatch(/^접수( |일 미정)/);

      // 마감 span 텍스트는 "마감 ..." 또는 "마감 미정" 형식.
      const deadlineText = (await deadline.innerText()).trim();
      expect(deadlineText).toMatch(/^마감/);

      // 접수 span 이 마감 span 보다 화면상 왼쪽(작은 x좌표)에 있다.
      const receivedBox = await received.boundingBox();
      const deadlineBox = await deadline.boundingBox();
      expect(receivedBox).not.toBeNull();
      expect(deadlineBox).not.toBeNull();
      if (receivedBox && deadlineBox) {
        expect(receivedBox.x).toBeLessThan(deadlineBox.x);
      }
    }
  });

  test("전이 행의 중복 배지 문구가 '...에도' 이고 '도마감' 처럼 깨진 표현이 없다", async ({
    page,
  }) => {
    await page.goto(DASHBOARD_URL);
    await page.waitForLoadState("networkidle");

    const sectionA = page.locator(".dashboard-section--a");
    await expect(sectionA).toBeVisible();

    // A 섹션 카드를 모두 펼친다.
    const cards = sectionA.locator("details.dashboard-card");
    const cardCount = await cards.count();
    for (let index = 0; index < cardCount; index += 1) {
      await cards.nth(index).evaluate((element) => {
        (element as HTMLDetailsElement).open = true;
      });
    }

    // 펼친 A 섹션 전체 텍스트에 깨진 전이 표현이 없어야 한다.
    // 과거 버그: "🔄 전이→접수중도" 가 옆 "마감 ..." 과 붙어 "도마감" 으로
    // 읽혔다 — 00135-1 에서 "에도" 어법으로 교정됨.
    const sectionText = await sectionA.innerText();
    expect(sectionText).not.toContain("전이→접수예정도");
    expect(sectionText).not.toContain("전이→접수중도");
    expect(sectionText).not.toContain("전이→마감도");

    // 중복 배지가 렌더된 경우 모두 "전이→{상태}에도" 형식이어야 한다.
    const duplicateBadges = sectionA.locator(".dashboard-card__duplicate-badge");
    const badgeCount = await duplicateBadges.count();
    for (let index = 0; index < badgeCount; index += 1) {
      const badgeText = (await duplicateBadges.nth(index).innerText()).trim();
      if (badgeText.includes("전이→")) {
        expect(badgeText).toMatch(/전이→(접수예정|접수중|마감)에도$/);
      }
    }
  });
});
