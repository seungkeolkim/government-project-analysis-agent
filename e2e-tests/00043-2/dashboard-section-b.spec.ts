import { test, expect } from "@playwright/test";

const DASHBOARD_URL = "http://localhost:8000/dashboard";

test.describe("B 섹션 레이아웃 — 2컬럼→2행 stack", () => {
  test("dashboard-section-b__rows 컨테이너가 존재하고 __columns 는 없음", async ({
    page,
  }) => {
    await page.goto(DASHBOARD_URL);

    // 새 2행 컨테이너 클래스 존재
    await expect(page.locator(".dashboard-section-b__rows")).toBeVisible();

    // 구 2컬럼 클래스 없음
    await expect(page.locator(".dashboard-section-b__columns")).toHaveCount(0);
  });

  test("두 그룹이 세로(column 방향)로 쌓임 — soon_to_open 이 soon_to_close 위에 위치", async ({
    page,
  }) => {
    await page.goto(DASHBOARD_URL);

    const openGroup = page.locator("[data-section-b-group=soon_to_open]");
    const closeGroup = page.locator("[data-section-b-group=soon_to_close]");

    const openBox = await openGroup.boundingBox();
    const closeBox = await closeGroup.boundingBox();

    expect(openBox).not.toBeNull();
    expect(closeBox).not.toBeNull();

    // soon_to_open 의 bottom 이 soon_to_close 의 top 보다 위에 있어야 함 (세로 stack)
    expect(openBox!.y + openBox!.height).toBeLessThanOrEqual(closeBox!.y + 10);

    // 두 그룹의 폭이 거의 같아야 함 (전체 폭을 넓게 사용)
    expect(Math.abs(openBox!.width - closeBox!.width)).toBeLessThan(20);
  });
});

test.describe("B 섹션 — 건수 표시", () => {
  test("'조만간 접수될 공고' 건수가 (N건) 형태로 표시됨", async ({ page }) => {
    await page.goto(DASHBOARD_URL);

    const openCount = page.locator(
      "[data-section-b-count=soon_to_open]"
    );
    await expect(openCount).toBeVisible();

    const text = await openCount.textContent();
    expect(text).toMatch(/^\(\d+건\)$/);

    // 건수가 0보다 커야 함 (DB에 데이터가 있으므로)
    const match = text!.match(/\((\d+)건\)/);
    expect(parseInt(match![1])).toBeGreaterThan(0);
  });

  test("'조만간 마감될 공고' 건수가 (N건) 형태로 표시됨", async ({ page }) => {
    await page.goto(DASHBOARD_URL);

    const closeCount = page.locator(
      "[data-section-b-count=soon_to_close]"
    );
    await expect(closeCount).toBeVisible();

    const text = await closeCount.textContent();
    expect(text).toMatch(/^\(\d+건\)$/);

    const match = text!.match(/\((\d+)건\)/);
    expect(parseInt(match![1])).toBeGreaterThan(0);
  });
});

test.describe("B 섹션 — D-Day 배지", () => {
  test("soon_to_open 첫 번째 row 에 D-Day 배지가 표시됨", async ({ page }) => {
    await page.goto(DASHBOARD_URL);

    const firstOpenDDay = page
      .locator("[data-section-b-group=soon_to_open] [data-section-b-d-day]")
      .first();
    await expect(firstOpenDDay).toBeVisible();

    const label = await firstOpenDDay.textContent();
    // D-Day, D-1, D-2, D-10 등 형식
    expect(label?.trim()).toMatch(/^(D-Day|D-\d+|D\+\d+)$/);
  });

  test("soon_to_close 첫 번째 row 에 D-Day 배지가 표시됨", async ({ page }) => {
    await page.goto(DASHBOARD_URL);

    const firstCloseDDay = page
      .locator("[data-section-b-group=soon_to_close] [data-section-b-d-day]")
      .first();
    await expect(firstCloseDDay).toBeVisible();

    const label = await firstCloseDDay.textContent();
    expect(label?.trim()).toMatch(/^(D-Day|D-\d+|D\+\d+)$/);
  });

  test("모든 soon_to_open row 의 D-Day 배지가 비어있지 않음", async ({
    page,
  }) => {
    await page.goto(DASHBOARD_URL);

    const dDaySpans = page.locator(
      "[data-section-b-group=soon_to_open] [data-section-b-d-day]"
    );
    const count = await dDaySpans.count();
    expect(count).toBeGreaterThan(0);

    for (let i = 0; i < Math.min(count, 5); i++) {
      const text = await dDaySpans.nth(i).textContent();
      expect(text?.trim().length).toBeGreaterThan(0);
    }
  });
});

test.describe("B 섹션 — 접수/마감 날짜 강조 분리", () => {
  test("soon_to_open row: received_at 이 --received 클래스로 강조됨", async ({
    page,
  }) => {
    await page.goto(DASHBOARD_URL);

    const receivedDate = page
      .locator(
        "[data-section-b-group=soon_to_open] .dashboard-section-b__date--received"
      )
      .first();
    await expect(receivedDate).toBeVisible();

    const text = await receivedDate.textContent();
    expect(text?.trim()).toMatch(/^접수\s+\d{4}-\d{2}-\d{2}$/);
  });

  test("soon_to_open row: deadline_at 이 --muted 클래스로 강조 해제됨 (--deadline 아님)", async ({
    page,
  }) => {
    await page.goto(DASHBOARD_URL);

    // soon_to_open 그룹 안에 --deadline 클래스가 없어야 함
    const deadlineInOpen = page.locator(
      "[data-section-b-group=soon_to_open] .dashboard-section-b__date--deadline"
    );
    await expect(deadlineInOpen).toHaveCount(0);

    // --muted 클래스가 있어야 함
    const mutedDate = page
      .locator(
        "[data-section-b-group=soon_to_open] .dashboard-section-b__date--muted"
      )
      .first();
    await expect(mutedDate).toBeVisible();
    const text = await mutedDate.textContent();
    expect(text?.trim()).toMatch(/^마감\s+\d{4}-\d{2}-\d{2}$/);
  });

  test("soon_to_close row: deadline_at 이 --deadline 클래스로 강조됨 (빨간 강조 유지)", async ({
    page,
  }) => {
    await page.goto(DASHBOARD_URL);

    const deadlineDate = page
      .locator(
        "[data-section-b-group=soon_to_close] .dashboard-section-b__date--deadline"
      )
      .first();
    await expect(deadlineDate).toBeVisible();

    const text = await deadlineDate.textContent();
    expect(text?.trim()).toMatch(/^마감\s+\d{4}-\d{2}-\d{2}$/);
  });

  test("soon_to_close row: --received 또는 --muted 클래스가 없음", async ({
    page,
  }) => {
    await page.goto(DASHBOARD_URL);

    // soon_to_close 그룹은 --received 없음
    const receivedInClose = page.locator(
      "[data-section-b-group=soon_to_close] .dashboard-section-b__date--received"
    );
    await expect(receivedInClose).toHaveCount(0);
  });
});
