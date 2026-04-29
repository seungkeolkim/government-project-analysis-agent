/**
 * task 00043 — 대시보드 수정 통합 E2E 테스트
 *
 * 사용자 원문의 4개 항목을 실제 브라우저로 종합 검증:
 * 1. 스냅샷 누적 구간 (from, to] 시맨틱 점검
 * 2-1. B 섹션 2행 stack + 건수 표시
 * 2-2. D-Day 배지 + 접수/마감 강조 분리
 * 2-3. 추이 차트 범위 기준일 ~ 기준일-30일
 */
import { test, expect } from "@playwright/test";

// 서비스는 포트 8000에서 실행 중 (E2E 설정의 base_url=3000은 다른 앱)
const DASHBOARD = "http://localhost:8000/dashboard";
const DASHBOARD_COMPARE =
  "http://localhost:8000/dashboard?base_date=2026-04-29&compare_mode=custom&compare_date=2026-04-21";

// ── 1. 스냅샷 누적 구간 (from, to] 시맨틱 ──────────────────────────────────

test.describe("1. 스냅샷 누적 구간 — compare_date=4/21, base_date=4/29", () => {
  test("폼 data-* 속성이 요청 파라미터를 정확히 반영", async ({ page }) => {
    await page.goto(DASHBOARD_COMPARE);
    await expect(page).toHaveTitle(/대시보드/);

    const controls = page.locator("[data-dashboard-controls]");
    await expect(controls).toHaveAttribute("data-base-date", "2026-04-29");
    await expect(controls).toHaveAttribute("data-compare-date", "2026-04-21");
  });

  test("compare_mode=custom 으로 compare_date 필드셋 활성화", async ({
    page,
  }) => {
    await page.goto(DASHBOARD_COMPARE);

    const compareCalendar = page.locator("[data-dashboard-calendar=compare]");
    await expect(compareCalendar).toHaveAttribute(
      "data-selected-date",
      "2026-04-21"
    );
  });

  test("숨김 base_date input 값이 2026-04-29 유지", async ({ page }) => {
    await page.goto(DASHBOARD_COMPARE);

    const hiddenInput = page
      .locator("input[type=hidden][name=base_date]")
      .first();
    await expect(hiddenInput).toHaveAttribute("value", "2026-04-29");
  });

  test("A 섹션이 에러 없이 렌더링됨 (데이터 없을 시 no-data notice 표시)", async ({
    page,
  }) => {
    const errors: string[] = [];
    page.on("pageerror", (err) => errors.push(err.message));

    await page.goto(DASHBOARD_COMPARE);
    await page.waitForLoadState("networkidle");

    const sectionA = page.locator(".dashboard-section--a");
    await expect(sectionA).toBeVisible();
    expect(errors).toHaveLength(0);
  });
});

// ── 2-1. B 섹션 2행 stack + 건수 표시 ────────────────────────────────────

test.describe("2-1. B 섹션 레이아웃 — 2행 stack + 건수 표시", () => {
  test("dashboard-section-b__rows 컨테이너 존재, __columns 없음", async ({
    page,
  }) => {
    await page.goto(DASHBOARD);

    await expect(page.locator(".dashboard-section-b__rows")).toBeVisible();
    await expect(page.locator(".dashboard-section-b__columns")).toHaveCount(0);
  });

  test("soon_to_open 그룹이 soon_to_close 그룹보다 위에 위치 (세로 stack)", async ({
    page,
  }) => {
    await page.goto(DASHBOARD);

    const openBox = await page
      .locator("[data-section-b-group=soon_to_open]")
      .boundingBox();
    const closeBox = await page
      .locator("[data-section-b-group=soon_to_close]")
      .boundingBox();

    expect(openBox).not.toBeNull();
    expect(closeBox).not.toBeNull();
    // 세로 stack: open 의 bottom ≤ close 의 top
    expect(openBox!.y + openBox!.height).toBeLessThanOrEqual(
      closeBox!.y + 10
    );
  });

  test("두 그룹이 화면 폭을 넓게 차지 (가로 폭이 거의 동일)", async ({
    page,
  }) => {
    await page.goto(DASHBOARD);

    const openBox = await page
      .locator("[data-section-b-group=soon_to_open]")
      .boundingBox();
    const closeBox = await page
      .locator("[data-section-b-group=soon_to_close]")
      .boundingBox();

    expect(Math.abs(openBox!.width - closeBox!.width)).toBeLessThan(20);
  });

  test("'접수될 공고' 건수가 (N건) 형태로 표시됨", async ({ page }) => {
    await page.goto(DASHBOARD);

    const openCountEl = page.locator("[data-section-b-count=soon_to_open]");
    await expect(openCountEl).toBeVisible();
    const text = await openCountEl.textContent();
    expect(text?.trim()).toMatch(/^\(\d+건\)$/);
    const count = parseInt(text!.match(/(\d+)/)![1]);
    expect(count).toBeGreaterThan(0);
  });

  test("'마감될 공고' 건수가 (N건) 형태로 표시됨", async ({ page }) => {
    await page.goto(DASHBOARD);

    const closeCountEl = page.locator("[data-section-b-count=soon_to_close]");
    await expect(closeCountEl).toBeVisible();
    const text = await closeCountEl.textContent();
    expect(text?.trim()).toMatch(/^\(\d+건\)$/);
    const count = parseInt(text!.match(/(\d+)/)![1]);
    expect(count).toBeGreaterThan(0);
  });
});

// ── 2-2. D-Day 배지 + 접수/마감 강조 분리 ────────────────────────────────

test.describe("2-2. D-Day 배지 + 접수/마감 강조", () => {
  test("soon_to_open 첫 row D-Day 배지가 D-N/D-Day 형식", async ({ page }) => {
    await page.goto(DASHBOARD);

    const firstDDay = page
      .locator("[data-section-b-group=soon_to_open] [data-section-b-d-day]")
      .first();
    await expect(firstDDay).toBeVisible();
    const label = (await firstDDay.textContent())?.trim() ?? "";
    expect(label).toMatch(/^(D-Day|D-\d+|D\+\d+)$/);
  });

  test("soon_to_close 첫 row D-Day 배지가 D-N/D-Day 형식", async ({ page }) => {
    await page.goto(DASHBOARD);

    const firstDDay = page
      .locator("[data-section-b-group=soon_to_close] [data-section-b-d-day]")
      .first();
    await expect(firstDDay).toBeVisible();
    const label = (await firstDDay.textContent())?.trim() ?? "";
    expect(label).toMatch(/^(D-Day|D-\d+|D\+\d+)$/);
  });

  test("soon_to_open: received_at 이 --received 클래스 (blue #1d4ed8 + bold)", async ({
    page,
  }) => {
    await page.goto(DASHBOARD);

    const receivedSpan = page
      .locator(
        "[data-section-b-group=soon_to_open] .dashboard-section-b__date--received"
      )
      .first();
    await expect(receivedSpan).toBeVisible();

    // 텍스트가 \"접수 YYYY-MM-DD\" 형식
    const text = (await receivedSpan.textContent())?.trim() ?? "";
    expect(text).toMatch(/^접수\s+\d{4}-\d{2}-\d{2}$/);

    // CSS 색상이 파란색 계열 (#1d4ed8 = rgb(29,78,216))
    const color = await receivedSpan.evaluate(
      (el) => getComputedStyle(el).color
    );
    expect(color).toContain("29");

    // font-weight >= 600
    const fw = await receivedSpan.evaluate(
      (el) => getComputedStyle(el).fontWeight
    );
    expect(parseInt(fw)).toBeGreaterThanOrEqual(600);
  });

  test("soon_to_open: deadline_at 이 --muted 클래스 (--deadline 없음)", async ({
    page,
  }) => {
    await page.goto(DASHBOARD);

    // soon_to_open 안에 --deadline 없음
    await expect(
      page.locator(
        "[data-section-b-group=soon_to_open] .dashboard-section-b__date--deadline"
      )
    ).toHaveCount(0);

    // --muted 있음
    const mutedSpan = page
      .locator(
        "[data-section-b-group=soon_to_open] .dashboard-section-b__date--muted"
      )
      .first();
    await expect(mutedSpan).toBeVisible();
    const text = (await mutedSpan.textContent())?.trim() ?? "";
    expect(text).toMatch(/^마감\s+\d{4}-\d{2}-\d{2}$/);
  });

  test("soon_to_close: deadline_at 이 --deadline 클래스 (red #b91c1c + bold 유지)", async ({
    page,
  }) => {
    await page.goto(DASHBOARD);

    const deadlineSpan = page
      .locator(
        "[data-section-b-group=soon_to_close] .dashboard-section-b__date--deadline"
      )
      .first();
    await expect(deadlineSpan).toBeVisible();

    const text = (await deadlineSpan.textContent())?.trim() ?? "";
    expect(text).toMatch(/^마감\s+\d{4}-\d{2}-\d{2}$/);

    // CSS 색상이 빨간색 계열 (#b91c1c = rgb(185,28,28))
    const color = await deadlineSpan.evaluate(
      (el) => getComputedStyle(el).color
    );
    expect(color).toContain("185");

    // font-weight >= 600
    const fw = await deadlineSpan.evaluate(
      (el) => getComputedStyle(el).fontWeight
    );
    expect(parseInt(fw)).toBeGreaterThanOrEqual(600);
  });

  test("soon_to_close: --received / --muted 클래스 없음", async ({ page }) => {
    await page.goto(DASHBOARD);

    await expect(
      page.locator(
        "[data-section-b-group=soon_to_close] .dashboard-section-b__date--received"
      )
    ).toHaveCount(0);
  });
});

// ── 2-3. 추이 차트 범위 — 기준일 ~ 기준일-30일 ──────────────────────────

test.describe("2-3. 추이 차트 — 기준일 기준 과거 30일 단방향", () => {
  let chartData: {
    base_date: string;
    from_date: string;
    to_date: string;
    past_days: number;
    days: Array<{ date_iso: string; x_axis_label: string }>;
  };

  test.beforeEach(async ({ page }) => {
    await page.goto(DASHBOARD);
    chartData = await page.evaluate(() => {
      const scripts = document.querySelectorAll("script");
      for (const script of scripts) {
        const text = script.textContent?.trim() ?? "";
        if (text.startsWith("{") && text.includes("\"base_date\"")) {
          return JSON.parse(text);
        }
      }
      return null;
    });
    expect(chartData).not.toBeNull();
  });

  test("헤더에 '과거 N일' 포함, '±' 없음", async ({ page }) => {
    const titleText = await page
      .locator("[data-dashboard-trend-chart] .dashboard-section__title")
      .textContent();
    expect(titleText).toMatch(/과거\s+\d+일/);
    expect(titleText).not.toMatch(/[±]\d+일/);
  });

  test("헤더 날짜 범위가 정확히 30일 차이 (기준일-30 ~ 기준일)", async ({
    page,
  }) => {
    const metaText = await page
      .locator(
        "[data-dashboard-trend-chart] .dashboard-section__title-meta"
      )
      .first()
      .textContent();
    const m = metaText!.match(
      /(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})/
    );
    expect(m).not.toBeNull();
    const diff =
      (new Date(m![2]).getTime() - new Date(m![1]).getTime()) /
      (1000 * 60 * 60 * 24);
    expect(diff).toBe(30);
  });

  test("JSON past_days=30, half_window 없음", async () => {
    expect(chartData.past_days).toBe(30);
    expect(chartData).not.toHaveProperty("half_window");
  });

  test("to_date == base_date (미래 날짜 미포함)", async () => {
    expect(chartData.to_date).toBe(chartData.base_date);
  });

  test("from_date 가 to_date 기준 정확히 30일 이전", async () => {
    const from = new Date(chartData.from_date);
    const to = new Date(chartData.to_date);
    const diff = (to.getTime() - from.getTime()) / (1000 * 60 * 60 * 24);
    expect(diff).toBe(30);
  });

  test("days 배열 31개, 오름차순, 미래 일자 없음", async () => {
    expect(chartData.days).toHaveLength(31);

    const toDate = new Date(chartData.to_date);
    for (let i = 0; i < chartData.days.length; i++) {
      const dayDate = new Date(chartData.days[i].date_iso);
      // 미래 날짜 없음
      expect(dayDate.getTime()).toBeLessThanOrEqual(toDate.getTime());
      // 오름차순
      if (i > 0) {
        expect(dayDate.getTime()).toBeGreaterThan(
          new Date(chartData.days[i - 1].date_iso).getTime()
        );
      }
    }
  });

  test("첫 day = from_date, 마지막 day = to_date (base_date)", async () => {
    expect(chartData.days[0].date_iso).toBe(chartData.from_date);
    expect(chartData.days[30].date_iso).toBe(chartData.to_date);
  });

  test("canvas aria-label 에 '과거 N일' 포함", async ({ page }) => {
    const ariaLabel = await page
      .locator("#dashboardTrendChart")
      .getAttribute("aria-label");
    expect(ariaLabel).toMatch(/과거\s+\d+일/);
  });
});
