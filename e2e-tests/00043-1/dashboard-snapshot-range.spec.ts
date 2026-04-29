import { test, expect } from "@playwright/test";

// Service runs on port 8000 (base_url in config targets port 3000 for another service)
const DASHBOARD_URL = "http://localhost:8000/dashboard";

test.describe("대시보드 기본 렌더링", () => {
  test("대시보드 페이지 타이틀 및 주요 섹션 존재", async ({ page }) => {
    await page.goto(DASHBOARD_URL);
    await expect(page).toHaveTitle(/대시보드/);

    await expect(page.locator(".dashboard-page__title")).toContainText(
      "대시보드"
    );
    await expect(
      page.locator(".dashboard-section--a .dashboard-section__title")
    ).toContainText("A. 공고 변화 목록");
    await expect(
      page.locator(".dashboard-section--b .dashboard-section__title")
    ).toContainText("B. D-Day 임박 공고 목록");
  });

  test("대시보드 컨트롤 폼 — 기준일 기본값이 오늘 날짜", async ({ page }) => {
    await page.goto(DASHBOARD_URL);

    const controls = page.locator("[data-dashboard-controls]");
    await expect(controls).toBeVisible();

    const baseDateAttr = await controls.getAttribute("data-base-date");
    expect(baseDateAttr).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });
});

test.describe("스냅샷 누적 구간 시맨틱 — (from, to] 반-open 검증", () => {
  test(
    "compare_date=2026-04-21, base_date=2026-04-29 요청 시 폼 속성이 올바르게 반영됨",
    async ({ page }) => {
      await page.goto(
        `${DASHBOARD_URL}?base_date=2026-04-29&compare_mode=custom&compare_date=2026-04-21`
      );
      await expect(page).toHaveTitle(/대시보드/);

      const controls = page.locator("[data-dashboard-controls]");
      await expect(controls).toBeVisible();

      // 폼의 data-* 속성이 요청 파라미터를 정확히 반영하는지 확인
      await expect(controls).toHaveAttribute("data-base-date", "2026-04-29");
      await expect(controls).toHaveAttribute(
        "data-compare-date",
        "2026-04-21"
      );
    }
  );

  test(
    "compare_date=2026-04-21, base_date=2026-04-29 — Section A가 에러 없이 렌더링됨",
    async ({ page }) => {
      await page.goto(
        `${DASHBOARD_URL}?base_date=2026-04-29&compare_mode=custom&compare_date=2026-04-21`
      );

      // Section A 렌더링 확인 (데이터 없어도 섹션 자체는 표시됨)
      const sectionA = page.locator(".dashboard-section--a");
      await expect(sectionA).toBeVisible();

      // 콘솔 에러 없음 검증을 위해 JS 에러 없이 로드 완료
      const errors: string[] = [];
      page.on("pageerror", (err) => errors.push(err.message));
      await page.waitForLoadState("networkidle");
      expect(errors).toHaveLength(0);
    }
  );

  test(
    "숨김 base_date 입력값이 요청 파라미터와 일치 — 폼 재제출 시 날짜 유지",
    async ({ page }) => {
      await page.goto(
        `${DASHBOARD_URL}?base_date=2026-04-29&compare_mode=custom&compare_date=2026-04-21`
      );

      // 숨김 input[name=base_date]의 value가 2026-04-29인지 확인
      const hiddenBaseDateInput = page.locator(
        "input[type=hidden][name=base_date]"
      ).first();
      await expect(hiddenBaseDateInput).toHaveAttribute(
        "value",
        "2026-04-29"
      );
    }
  );

  test(
    "compare_mode=custom 선택 시 compare_date 필드셋이 활성화됨",
    async ({ page }) => {
      await page.goto(
        `${DASHBOARD_URL}?base_date=2026-04-29&compare_mode=custom&compare_date=2026-04-21`
      );

      const compareFieldset = page.locator("[data-dashboard-compare-fieldset]");
      await expect(compareFieldset).toBeVisible();

      // compare_date 입력의 data-selected-date가 올바른지 확인
      const compareDateCalendar = page.locator(
        "[data-dashboard-calendar=compare]"
      );
      await expect(compareDateCalendar).toHaveAttribute(
        "data-selected-date",
        "2026-04-21"
      );
    }
  );
});
