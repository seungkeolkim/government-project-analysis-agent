/*
 * 대시보드 추이 차트 부트스트랩 (Phase 5b / task 00042-6).
 *
 * 책임:
 *   - #dashboardTrendChartData JSON 을 파싱.
 *   - #dashboardTrendChart canvas 위에 Chart.js line chart 인스턴스를 만든다.
 *   - 3개 series: 신규 / 내용 변경 / 전이 (전이는 접수예정 + 접수중 + 마감 합계).
 *   - x축 라벨은 서버가 박은 'MM-DD' (KST 기준) 그대로 사용.
 *
 * 데이터는 서버 사전 계산 후 JSON 임베드 (사용자 원문 §12 / design doc §9.2) —
 * 별도 fetch / API 호출 없음.
 *
 * Chart.js 가 로드되지 않은 경우(파일 누락 / 네트워크 차단) 캔버스만 빈 채로
 * 남고 콘솔에 경고가 찍힌다 — 페이지 자체는 정상 동작 (대시보드 read-only).
 */

(function () {
    "use strict";

    /**
     * #dashboardTrendChartData 의 textContent 를 JSON.parse 한다.
     * @returns {object|null}
     */
    function readEmbeddedTrendData() {
        var dataElement = document.getElementById("dashboardTrendChartData");
        if (!dataElement) {
            return null;
        }
        var rawText = dataElement.textContent.trim();
        if (!rawText) {
            return null;
        }
        try {
            return JSON.parse(rawText);
        } catch (parseError) {
            console.warn("dashboard_trend_chart: JSON 파싱 실패", parseError);
            return null;
        }
    }

    /**
     * Chart.js 가 로드되었는지 확인. 글로벌 ``Chart`` 가 없으면 false.
     * @returns {boolean}
     */
    function isChartJsLoaded() {
        return typeof window !== "undefined" && typeof window.Chart === "function";
    }

    /**
     * day-by-day 데이터에서 각 series 의 카운트 배열을 만든다.
     * @param {object[]} days
     * @returns {{labels: string[], newCounts: number[], contentChangedCounts: number[], transitionedCounts: number[]}}
     */
    function buildSeriesArrays(days) {
        var labels = [];
        var newCounts = [];
        var contentChangedCounts = [];
        var transitionedCounts = [];
        for (var index = 0; index < days.length; index++) {
            var point = days[index];
            labels.push(point.x_axis_label);
            newCounts.push(point.new_count);
            contentChangedCounts.push(point.content_changed_count);
            transitionedCounts.push(point.transitioned_count);
        }
        return {
            labels: labels,
            newCounts: newCounts,
            contentChangedCounts: contentChangedCounts,
            transitionedCounts: transitionedCounts,
        };
    }

    /**
     * Chart.js 인스턴스를 만든다.
     * @param {HTMLCanvasElement} canvas
     * @param {object} trendData
     */
    function createChart(canvas, trendData) {
        var series = buildSeriesArrays(trendData.days || []);
        // Chart.js v4 기본 line chart 옵션 — 인터랙션 (zoom/brush) 은 design doc §1.2
        // '범위 밖' 이라 비활성. tooltip / legend 만 기본 동작.
        return new window.Chart(canvas, {
            type: "line",
            data: {
                labels: series.labels,
                datasets: [
                    {
                        label: "신규",
                        data: series.newCounts,
                        borderColor: "#1d4ed8",
                        backgroundColor: "rgba(29, 78, 216, 0.15)",
                        borderWidth: 2,
                        tension: 0.2,
                        pointRadius: 2,
                    },
                    {
                        label: "내용 변경",
                        data: series.contentChangedCounts,
                        borderColor: "#b45309",
                        backgroundColor: "rgba(180, 83, 9, 0.15)",
                        borderWidth: 2,
                        tension: 0.2,
                        pointRadius: 2,
                    },
                    {
                        label: "전이",
                        data: series.transitionedCounts,
                        borderColor: "#15803d",
                        backgroundColor: "rgba(21, 128, 61, 0.15)",
                        borderWidth: 2,
                        tension: 0.2,
                        pointRadius: 2,
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: {
                    mode: "index",
                    intersect: false,
                },
                scales: {
                    y: {
                        beginAtZero: true,
                        ticks: {
                            // 정수 카운트만 — 0 / 1 / 2 ... step 1.
                            stepSize: 1,
                            precision: 0,
                        },
                    },
                    x: {
                        ticks: {
                            // 31개 라벨이 빽빽해질 수 있어 자동 truncate.
                            autoSkip: true,
                            maxTicksLimit: 12,
                        },
                    },
                },
                plugins: {
                    legend: {
                        position: "top",
                    },
                    tooltip: {
                        callbacks: {
                            // 가운데 클릭 새 창 등 추가 인터랙션은 본 task 범위 밖.
                            title: function (items) {
                                if (!items || !items.length) {
                                    return "";
                                }
                                return items[0].label;
                            },
                        },
                    },
                },
            },
        });
    }

    function bootstrap() {
        var canvas = document.getElementById("dashboardTrendChart");
        if (!canvas) {
            // 대시보드 페이지가 아니면 early-return — base.html 의 다른 페이지에서
            // 본 스크립트가 로드돼도 영향 없음.
            return;
        }

        var trendData = readEmbeddedTrendData();
        if (!trendData) {
            console.warn("dashboard_trend_chart: 임베드 데이터를 찾지 못함 — 차트 미생성");
            return;
        }

        if (!isChartJsLoaded()) {
            // Chart.js vendor 번들이 없거나 로드 실패. 본 task 의 대시보드는 read-only
            // 라 차트가 빠져도 페이지는 동작. 콘솔에만 안내하고 종료.
            console.warn("dashboard_trend_chart: Chart.js 미로드 — 차트 미생성");
            return;
        }

        try {
            createChart(canvas, trendData);
        } catch (chartError) {
            console.error("dashboard_trend_chart: Chart 생성 실패", chartError);
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", bootstrap);
    } else {
        bootstrap();
    }
})();
