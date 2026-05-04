/*
 * 건의사항 수용 여부 체크/수정 모달 (task 00051-5).
 *
 * 책임:
 *   - "수용 여부 체크/수정" 버튼 클릭 → <dialog>.showModal() 호출.
 *   - 취소 버튼 클릭 → <dialog>.close() 로 닫기.
 *   - 수용 여부 라디오 변경 → "수용" 또는 "일부수용" 일 때만 예상 완료일
 *     <input type="date"> 를 활성화. 그 외(검토중/거절) 상태에서는 비활성화 +
 *     입력값 초기화.
 *
 * 설계 메모:
 *   - 모든 select 가 null 일 수 있으므로(관리자 외 사용자에게는 모달이 렌더되지
 *     않음) 진입 즉시 early-return 한다 — 다른 페이지에서 본 스크립트가 우연히
 *     로드되어도 안전하다.
 *   - native <dialog> 의 폴백은 두지 않는다(modern 브라우저 전제). 호환성이
 *     이슈면 후속 task 에서 polyfill 도입.
 */

(function () {
    "use strict";

    /**
     * 수용 여부 라디오 값에 따라 예상 완료일 입력의 활성/비활성을 동기화한다.
     *
     * @param {NodeListOf<HTMLInputElement>} radios 수용 여부 라디오 그룹.
     * @param {HTMLInputElement} dateInput 예상 완료일 input[type="date"].
     */
    function syncExpectedCompletionDateState(radios, dateInput) {
        var checkedValue = "";
        for (var i = 0; i < radios.length; i++) {
            if (radios[i].checked) {
                checkedValue = radios[i].value;
                break;
            }
        }
        var shouldEnable = checkedValue === "수용" || checkedValue === "일부수용";
        dateInput.disabled = !shouldEnable;
        if (!shouldEnable) {
            // 비활성 상태에서 stale 값이 폼 submit 으로 흘러들어가지 않도록 초기화.
            // 라우트도 None 으로 강제 정규화하지만 UI 일관성을 위해 함께 비운다.
            dateInput.value = "";
        }
    }

    /**
     * 관리자 수용 여부 모달의 이벤트 바인딩을 설정한다.
     */
    function initSuggestionAcceptanceModal() {
        var openButton = document.getElementById("suggestion-acceptance-open");
        var dialog = document.getElementById("suggestion-acceptance-modal");
        var cancelButton = document.getElementById("suggestion-acceptance-cancel");
        var dateInput = document.getElementById("suggestion-acceptance-date");

        if (!openButton || !dialog || !cancelButton || !dateInput) {
            // 비관리자 페이지 또는 본 스크립트가 다른 페이지에서 로드된 경우 — 안전 종료.
            return;
        }

        var radios = dialog.querySelectorAll("input[name=\"acceptance_status\"]");
        if (radios.length === 0) {
            return;
        }

        openButton.addEventListener("click", function () {
            // showModal 은 폴리필 없는 브라우저에서 TypeError. 진입 시점에서
            // 한 번만 try/catch 해 사용성을 깎지 않도록 한다.
            try {
                dialog.showModal();
            } catch (err) {
                // <dialog> 미지원 폴백 — 일반 박스로 노출. 스타일은 CSS 가
                // [open] 속성으로 처리.
                dialog.setAttribute("open", "");
            }
        });

        cancelButton.addEventListener("click", function () {
            try {
                dialog.close();
            } catch (err) {
                dialog.removeAttribute("open");
            }
        });

        for (var i = 0; i < radios.length; i++) {
            radios[i].addEventListener("change", function () {
                syncExpectedCompletionDateState(radios, dateInput);
            });
        }

        // 초기 상태 동기화 — 페이지 로드 시 현재 저장값(checked) 에 맞춰
        // disabled / value 를 정렬한다.
        syncExpectedCompletionDateState(radios, dateInput);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initSuggestionAcceptanceModal);
    } else {
        initSuggestionAcceptanceModal();
    }
})();
