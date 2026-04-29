import { test, expect } from "@playwright/test";

const DASHBOARD_URL = "http://localhost:8000/dashboard";

test.describe("추이 차트 — 헤더 표기 (기준일 기준 과거 30일)", () => {
  test("헤더에 '과거 N일' 형식이 포함되고 '±' 표기가 없음", async ({ page }) => {
    await page.goto(DASHBOARD_URL);

    const trendSection = page.locator("[data-dashboard-trend-chart]");
    await expect(trendSection).toBeVisible();

    const titleText = await trendSection
      .locator(".dashboard-section__title")
      .textContent();

    // 새 형식: "과거 N일" 포함
    expect(titleText).toMatch(/과거\s+\d+일/);

    // 구 형식: "±N일" 없음
    expect(titleText).not.toMatch(/[±]\d+일/);
  });

  test("헤더의 날짜 범위가 과거 30일 (기준일-30 ~ 기준일)", async ({ page }) => {
    await page.goto(DASHBOARD_URL);

    const titleMeta = page
      .locator("[data-dashboard-trend-chart] .dashboard-section__title-meta")
      .first();
    await expect(titleMeta).toBeVisible();

    const metaText = await titleMeta.textContent();
    // "YYYY-MM-DD ~ YYYY-MM-DD" 형식
    const dateMatch = metaText!.match(
      /(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})/
    );
    expect(dateMatch).not.toBeNull();

    const fromDate = new Date(dateMatch![1]);
    const toDate = new Date(dateMatch![2]);

    // from_date 가 to_date 보다 과거여야 함
    expect(fromDate.getTime()).toBeLessThan(toDate.getTime());

    // 구간이 정확히 30일 차이여야 함
    const diffDays =
      (toDate.getTime() - fromDate.getTime()) / (1000 * 60 * 60 * 24);
    expect(diffDays).toBe(30);
  });

  test("canvas aria-label 에 '과거 N일' 포함", async ({ page }) => {
    await page.goto(DASHBOARD_URL);

    const canvas = page.locator("#dashboardTrendChart");
    await expect(canvas).toBeVisible();

    const ariaLabel = await canvas.getAttribute("aria-label");
    expect(ariaLabel).toMatch(/과거\s+\d+일/);
    expect(ariaLabel).not.toMatch(/[±]\d+일/);
  });
});

test.describe("추이 차트 — 임베딩 JSON 데이터 검증", () => {
  let chartData: {
    base_date: string;
    from_date: string;
    to_date: string;
    past_days: number;
    days: Array<{
      date_iso: string;
      x_axis_label: string;
      new_count: number;
      content_changed_count: number;
      transitioned_count: number;
    }>;
  };

  test.beforeEach(async ({ page }) => {
    await page.goto(DASHBOARD_URL);
    // 첫 번째 <script> 태그에서 JSON 데이터 추출
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

  test("JSON 에 past_days 필드가 있고 half_window 가 없음", async () => {
    expect(chartData).toHaveProperty("past_days");
    expect(chartData).not.toHaveProperty("half_window");
    expect(chartData.past_days).toBe(30);
  });

  test("to_date 가 base_date 와 일치 (미래 날짜 미포함)", async () => {
    expect(chartData.to_date).toBe(chartData.base_date);
  });

  test("from_date 가 to_date 기준 정확히 30일 이전", async () => {
    const from = new Date(chartData.from_date);
    const to = new Date(chartData.to_date);
    const diffDays = (to.getTime() - from.getTime()) / (1000 * 60 * 60 * 24);
    expect(diffDays).toBe(30);
  });

  test("days 배열이 총 31개 (기준일 포함 과거 30일)", async () => {
    expect(chartData.days).toHaveLength(31);
  });

  test("첫 번째 day 의 date_iso 가 from_date 와 일치", async () => {
    expect(chartData.days[0].date_iso).toBe(chartData.from_date);
  });

  test("마지막 day 의 date_iso 가 to_date (기준일) 와 일치", async () => {
    expect(chartData.days[30].date_iso).toBe(chartData.to_date);
  });

  test("미래 날짜 (기준일+1 이상) 가 days 배열에 없음", async () => {
    const toDate = new Date(chartData.to_date);
    for (const day of chartData.days) {
      const dayDate = new Date(day.date_iso);
      expect(dayDate.getTime()).toBeLessThanOrEqual(toDate.getTime());
    }
  });

  test("days 배열이 오름차순 날짜 정렬", async () => {
    for (let i = 1; i < chartData.days.length; i++) {
      const prev = new Date(chartData.days[i - 1].date_iso);
      const curr = new Date(chartData.days[i].date_iso);
      expect(curr.getTime()).toBeGreaterThan(prev.getTime());
    }
  });

  test("x_axis_label 이 'MM-DD' 형식이고 첫/마지막이 from/to date 와 일치", async () => {
    // first label
    const firstDay = chartData.days[0];
    const firstDateMmDd = chartData.from_date.slice(5).replace("-", "-"); // "MM-DD"
    expect(firstDay.x_axis_label).toBe(firstDateMmDd);

    // last label
    const lastDay = chartData.days[30];
    const lastDateMmDd = chartData.to_date.slice(5).replace("-", "-");
    expect(lastDay.x_axis_label).toBe(lastDateMmDd);
  });
});
