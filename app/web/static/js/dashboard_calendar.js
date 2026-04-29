/*
 * 대시보드 캘린더 컴포넌트 (Phase 5b / task 00042-2).
 *
 * 자체 구현 사유 (docs/dashboard_design.md §10.2):
 *   - 외부 CDN 의존 없음 컨벤션 + 본 task 의 캘린더 요구가 좁다 (월별 그리드 +
 *     가용 날짜 강조 + 비가용 click disabled).
 *   - Pikaday 같은 작은 라이브러리도 메인테너 활동 정지 + LICENSE 추가 부담.
 *
 * 동작:
 *   - 페이지 로드 시 #dashboardSnapshotDates 의 JSON 을 파싱해 가용 날짜 set
 *     을 만든다 (서버 사전 계산값 — 첫 렌더 깜빡임 방지).
 *   - data-dashboard-calendar 속성을 가진 div 마다 Calendar 인스턴스를 부착.
 *   - 가용 날짜는 동그라미 강조, 비가용은 흐리게 + click disabled. 사용자가
 *     가용 날짜를 클릭하면 폼의 hidden input 값을 갱신하고 form.submit() 한다.
 *   - 월 navigation (◀ ▶) 으로 다른 달 그리드를 다시 그린다.
 *   - 비교 모드 select 가 'custom' 으로 바뀌면 비교일 캘린더 fieldset 을
 *     보이도록 토글하고, 다른 모드로 바뀌면 즉시 form.submit() (서버에서
 *     compare_date 무시).
 */

(function () {
    "use strict";

    // ─────────────────────────────────────────────────────────────
    // 유틸 — 날짜 산술 (KST 가정 — 캘린더는 사용자 로컬 표시 ≠ KST 변환).
    //
    // 본 컴포넌트의 date 객체는 모두 '연/월/일 컴포넌트만' 의미를 가진다 —
    // tz 는 사용자 OS 의 로컬이지만, 화면에 그리는 라벨이 server-side KST
    // ISO 문자열과 1:1 매칭되도록 '날짜 컴포넌트 단순 비교' 로 다룬다.
    // 즉 자정 boundary 의 시간대 차이는 본 모듈 책임이 아니다 — 서버가
    // 가용 날짜 set 을 KST 기준 ISO 문자열로 내려보내기 때문에 클라이언트는
    // 같은 ISO 문자열을 만들어 비교만 하면 된다.
    // ─────────────────────────────────────────────────────────────

    /**
     * date.getFullYear/Month/Date 로 'YYYY-MM-DD' ISO 문자열을 만든다.
     * @param {Date} dateObj
     * @returns {string}
     */
    function formatIsoDate(dateObj) {
        var year = dateObj.getFullYear();
        var month = String(dateObj.getMonth() + 1).padStart(2, "0");
        var day = String(dateObj.getDate()).padStart(2, "0");
        return year + "-" + month + "-" + day;
    }

    /**
     * 'YYYY-MM-DD' 문자열을 같은 일자의 로컬 자정 Date 객체로 만든다.
     * 'new Date(isoString)' 은 'YYYY-MM-DD' 를 UTC 자정으로 해석하는데, 그러면
     * 사용자 OS 가 +09:00 라도 표시 일자가 하루 밀려 보일 위험이 있다 — 명시적
     * 컴포넌트 분해로 회피한다.
     * @param {string} iso
     * @returns {Date}
     */
    function parseIsoDate(iso) {
        var parts = iso.split("-");
        return new Date(parseInt(parts[0], 10), parseInt(parts[1], 10) - 1, parseInt(parts[2], 10));
    }

    /**
     * 한 달의 마지막 일을 반환 (1..31).
     * @param {number} year
     * @param {number} month  0-based month (Date 컨벤션과 동일).
     * @returns {number}
     */
    function lastDayOfMonth(year, month) {
        // 다음 달 0일은 이 달의 마지막 일.
        return new Date(year, month + 1, 0).getDate();
    }

    var DAY_LABELS = ["일", "월", "화", "수", "목", "금", "토"];

    // ─────────────────────────────────────────────────────────────
    // Calendar 컴포넌트
    // ─────────────────────────────────────────────────────────────

    /**
     * 단일 캘린더 인스턴스를 생성한다.
     *
     * @param {HTMLElement} rootElement              컨테이너 div.
     * @param {Set<string>} availableDateSet         가용 날짜 ISO 문자열 set.
     * @param {string|null} initialSelectedIso       초기 선택 일자 (없으면 null).
     * @param {function(string):void} onDateSelected 가용 날짜 클릭 시 호출되는 콜백.
     */
    function Calendar(rootElement, availableDateSet, initialSelectedIso, onDateSelected) {
        this.rootElement = rootElement;
        this.availableDateSet = availableDateSet;
        this.selectedIso = initialSelectedIso || null;
        this.onDateSelected = onDateSelected;

        // 표시 중인 달의 1일 — month navigation 의 기준.
        var initialDate = this.selectedIso ? parseIsoDate(this.selectedIso) : new Date();
        this.viewYear = initialDate.getFullYear();
        this.viewMonth = initialDate.getMonth();

        this.render();
    }

    /**
     * 현재 viewYear / viewMonth 기준으로 그리드를 다시 그린다.
     */
    Calendar.prototype.render = function () {
        var fragment = document.createDocumentFragment();

        // ── 헤더 (◀ 2026-04 ▶) ───────────────────────────────────
        var header = document.createElement("div");
        header.className = "dashboard-calendar__header";

        var prevButton = document.createElement("button");
        prevButton.type = "button";
        prevButton.className = "dashboard-calendar__nav";
        prevButton.setAttribute("aria-label", "이전 달");
        prevButton.textContent = "◀";
        var self = this;
        prevButton.addEventListener("click", function (event) {
            // 폼 submit 을 막기 위해 명시적으로 stopPropagation + preventDefault.
            // 본 컨트롤은 폼 안에 있어 button 의 type=button 이 핵심이지만,
            // 옛 브라우저 안전망으로 두 가지 모두 호출한다.
            event.stopPropagation();
            event.preventDefault();
            self.shiftMonth(-1);
        });

        var monthLabel = document.createElement("span");
        monthLabel.className = "dashboard-calendar__month";
        monthLabel.textContent = this.viewYear + "-" + String(this.viewMonth + 1).padStart(2, "0");

        var nextButton = document.createElement("button");
        nextButton.type = "button";
        nextButton.className = "dashboard-calendar__nav";
        nextButton.setAttribute("aria-label", "다음 달");
        nextButton.textContent = "▶";
        nextButton.addEventListener("click", function (event) {
            event.stopPropagation();
            event.preventDefault();
            self.shiftMonth(1);
        });

        header.appendChild(prevButton);
        header.appendChild(monthLabel);
        header.appendChild(nextButton);
        fragment.appendChild(header);

        // ── 요일 헤더 ────────────────────────────────────────────
        var weekdayRow = document.createElement("div");
        weekdayRow.className = "dashboard-calendar__weekday-row";
        for (var dayIndex = 0; dayIndex < 7; dayIndex++) {
            var weekdayCell = document.createElement("span");
            weekdayCell.className = "dashboard-calendar__weekday";
            weekdayCell.textContent = DAY_LABELS[dayIndex];
            weekdayRow.appendChild(weekdayCell);
        }
        fragment.appendChild(weekdayRow);

        // ── 날짜 그리드 ──────────────────────────────────────────
        var grid = document.createElement("div");
        grid.className = "dashboard-calendar__grid";

        var firstOfMonth = new Date(this.viewYear, this.viewMonth, 1);
        var leadingBlankCount = firstOfMonth.getDay();    // 일요일=0 시작
        var totalDaysInMonth = lastDayOfMonth(this.viewYear, this.viewMonth);

        // 앞쪽 빈 칸.
        for (var blankIndex = 0; blankIndex < leadingBlankCount; blankIndex++) {
            var blankCell = document.createElement("span");
            blankCell.className = "dashboard-calendar__day dashboard-calendar__day--blank";
            blankCell.setAttribute("aria-hidden", "true");
            grid.appendChild(blankCell);
        }

        // 실제 날짜 셀.
        for (var dayNumber = 1; dayNumber <= totalDaysInMonth; dayNumber++) {
            var cellDate = new Date(this.viewYear, this.viewMonth, dayNumber);
            var iso = formatIsoDate(cellDate);
            var isAvailable = this.availableDateSet.has(iso);
            var isSelected = this.selectedIso === iso;

            var dayCell = document.createElement("button");
            dayCell.type = "button";
            dayCell.className = "dashboard-calendar__day";
            if (isAvailable) {
                dayCell.classList.add("dashboard-calendar__day--available");
            } else {
                dayCell.classList.add("dashboard-calendar__day--disabled");
                dayCell.disabled = true;
                dayCell.setAttribute("aria-disabled", "true");
            }
            if (isSelected) {
                dayCell.classList.add("dashboard-calendar__day--selected");
                dayCell.setAttribute("aria-pressed", "true");
            }
            dayCell.setAttribute("data-iso-date", iso);
            dayCell.textContent = String(dayNumber);

            if (isAvailable) {
                // 가용 날짜만 클릭 핸들러 등록 (closure 안전성을 위해 변수 캡처).
                (function (capturedIso) {
                    dayCell.addEventListener("click", function (event) {
                        event.stopPropagation();
                        event.preventDefault();
                        self.selectedIso = capturedIso;
                        self.onDateSelected(capturedIso);
                    });
                })(iso);
            }

            grid.appendChild(dayCell);
        }

        fragment.appendChild(grid);

        // ── 기존 자식 노드 제거 후 새 fragment 부착 ───────────────
        while (this.rootElement.firstChild) {
            this.rootElement.removeChild(this.rootElement.firstChild);
        }
        this.rootElement.appendChild(fragment);
    };

    /**
     * 표시 중인 달을 ±delta 만큼 이동한다.
     * @param {number} delta
     */
    Calendar.prototype.shiftMonth = function (delta) {
        this.viewMonth += delta;
        while (this.viewMonth < 0) {
            this.viewMonth += 12;
            this.viewYear -= 1;
        }
        while (this.viewMonth > 11) {
            this.viewMonth -= 12;
            this.viewYear += 1;
        }
        this.render();
    };

    // ─────────────────────────────────────────────────────────────
    // 초기화 — DOMContentLoaded 시점에 폼 / 캘린더 / select 핸들러 부착.
    // ─────────────────────────────────────────────────────────────

    function bootstrap() {
        var formElement = document.querySelector("form[data-dashboard-controls]");
        if (!formElement) {
            // 대시보드 페이지가 아니면 early-return — base.html 의 다른 페이지에서
            // 본 스크립트가 로드돼도 영향 없다.
            return;
        }

        // 가용 날짜 set 은 서버가 #dashboardSnapshotDates 에 임베드한 JSON 에서
        // 가져온다 (초기 깜빡임 방지). API 재호출은 본 subtask 에서 하지 않는다 —
        // snapshot 캘린더 동기화는 사용자 reload 시점에만 (사용자 원문 §12).
        var availableDateSet = new Set();
        var dateScript = document.getElementById("dashboardSnapshotDates");
        if (dateScript && dateScript.textContent.trim()) {
            try {
                var parsedDateList = JSON.parse(dateScript.textContent);
                if (Array.isArray(parsedDateList)) {
                    parsedDateList.forEach(function (iso) {
                        availableDateSet.add(iso);
                    });
                }
            } catch (parseError) {
                // 파싱 실패 시 set 은 비어 있어 모든 날짜가 비활성으로 표시된다 —
                // 사용자가 페이지 reload 로 복구할 수 있도록 콘솔에만 경고.
                console.warn("dashboard_calendar: snapshot dates JSON 파싱 실패", parseError);
            }
        }

        var baseDateInput = formElement.querySelector("[data-dashboard-base-date-input]");
        var compareDateInput = formElement.querySelector("[data-dashboard-compare-date-input]");
        var compareModeSelect = formElement.querySelector("[data-dashboard-compare-mode]");
        var compareFieldset = formElement.querySelector("[data-dashboard-compare-fieldset]");

        // 기준일 캘린더.
        var baseRoot = formElement.querySelector('[data-dashboard-calendar="base"]');
        if (baseRoot && baseDateInput) {
            new Calendar(
                baseRoot,
                availableDateSet,
                baseRoot.getAttribute("data-selected-date") || null,
                function (selectedIso) {
                    baseDateInput.value = selectedIso;
                    formElement.submit();
                }
            );
        }

        // 비교일 캘린더 (custom 모드일 때만 의미). compareFieldset 의 is-hidden
        // 클래스로 표시 토글하지만 인스턴스 자체는 항상 만들어 두어 모드 전환
        // 시 재렌더 비용이 없도록 한다.
        var compareRoot = formElement.querySelector('[data-dashboard-calendar="compare"]');
        if (compareRoot && compareDateInput) {
            new Calendar(
                compareRoot,
                availableDateSet,
                compareRoot.getAttribute("data-selected-date") || null,
                function (selectedIso) {
                    compareDateInput.value = selectedIso;
                    formElement.submit();
                }
            );
        }

        // compare_mode select 변경 핸들러:
        //   - custom 으로 바뀌면 비교일 캘린더 fieldset 을 보이게만 하고 submit
        //     은 하지 않는다 (사용자가 비교일을 클릭해야 비로소 (from,to) 가
        //     완성).
        //   - 다른 모드로 바뀌면 compare_date 를 비우고 즉시 submit — 라우트가
        //     해당 mode 로 (from, to) 를 산출.
        if (compareModeSelect) {
            compareModeSelect.addEventListener("change", function () {
                var newMode = compareModeSelect.value;
                if (newMode === "custom") {
                    if (compareFieldset) {
                        compareFieldset.classList.remove("is-hidden");
                    }
                    // submit 보류 — 사용자가 비교일을 골라야 의미가 생긴다.
                } else {
                    if (compareFieldset) {
                        compareFieldset.classList.add("is-hidden");
                    }
                    if (compareDateInput) {
                        compareDateInput.value = "";
                    }
                    formElement.submit();
                }
            });
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", bootstrap);
    } else {
        bootstrap();
    }
})();
