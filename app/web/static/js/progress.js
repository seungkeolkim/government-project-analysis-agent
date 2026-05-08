/* ============================================================
 * Phase C — 공고 진행 상태 / 선점 UI 스크립트 (task 00097-5)
 *
 * 역할:
 *   1) 목록 셀 (.pg-wrap) hover 툴팁 — Phase B 의 viewport 기준 fixed 좌표
 *      배치 패턴 (00088) 을 그대로 재사용한다.
 *   2) 상세 페이지 인라인 섹션 (.pg-detail-section) 의 본인 조직 row 컨트롤:
 *      - [저장] / [삭제] (기존 row): PATCH / DELETE 호출 후 페이지 reload.
 *      - '[ 우리 조직 입장 표명하기 ]' 버튼: 인라인 폼 토글.
 *      - 새 row 작성 [저장]: POST 호출 후 페이지 reload.
 *      - 선점 충돌(409) / 본인 소속 외(403) / 무소속(422) 등의 에러는 row 안에
 *        feedback 박스를 띄워 안내한다. 페이지 이동 없음.
 *
 * 보안 / 컨벤션:
 *   - <script> 블록 안 Jinja 태그 금지 — 데이터는 모두 data-* 로 받아온다.
 *   - 저장·삭제는 fetch + same-origin (FastAPI ensure_same_origin 통과).
 *   - DELETE 는 window.confirm() 으로 확인 (사용자 실수 방지).
 *
 * 의존성: 없음 (vanilla JS). FastAPI 의 JSONResponse 와 통신.
 * ============================================================ */
(function () {
    "use strict";

    // ── 1. hover 툴팁 + 셀 클릭 expand ─────────────────────

    /**
     * 모든 .pg-wrap 에 mouse / focus 이벤트를 등록해 fixed 레이어 툴팁을 붙이고,
     * .pg-wrap--clickable 셀에는 click + Enter/Space 키로 expand 행을 toggle 하는
     * 핸들러도 함께 등록한다. Phase B (relevance.js) 의 fixed 레이어 패턴과 동일.
     */
    function initProgressTooltips() {
        document.querySelectorAll(".pg-wrap").forEach(function (wrap) {
            var tooltip = wrap.querySelector(".pg-tooltip");
            if (tooltip) {
                wrap.addEventListener("mouseenter", function () {
                    showProgressTooltip(wrap, tooltip);
                });
                wrap.addEventListener("mouseleave", function () {
                    hideProgressTooltip(tooltip);
                });
                wrap.addEventListener("focusin", function () {
                    showProgressTooltip(wrap, tooltip);
                });
                wrap.addEventListener("focusout", function () {
                    hideProgressTooltip(tooltip);
                });
            }

            // 셀 expand 가 가능한 .pg-wrap--clickable 만 click / 키보드 핸들러 부착.
            // 빈 셀(em dash)은 .pg-wrap--clickable 클래스가 없어 클릭 무반응.
            if (wrap.classList.contains("pg-wrap--clickable")) {
                wrap.addEventListener("click", function (event) {
                    event.stopPropagation();
                    toggleProgressExpand(wrap);
                });
                wrap.addEventListener("keydown", function (event) {
                    // Enter / Space — 키보드 사용자도 expand 토글 가능.
                    if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        event.stopPropagation();
                        toggleProgressExpand(wrap);
                    }
                });
            }
        });
    }

    /**
     * .pg-wrap 의 data-expand-target 이 가리키는 hidden <tr> 의 표시 여부를 toggle 한다.
     * Phase 3b 의 siblings-toggle 과 동일한 단순 display 토글 패턴.
     *
     * @param {HTMLElement} wrap .pg-wrap 컨테이너
     */
    function toggleProgressExpand(wrap) {
        var targetId = wrap.dataset.expandTarget;
        if (!targetId) {
            return;
        }
        var row = document.getElementById(targetId);
        if (!row) {
            // expand 본문이 없을 수 있다 (예: detail rows 가 0 건). 시각적 변화 없이 종료.
            return;
        }
        var isHidden = row.style.display === "none" || row.style.display === "";
        // siblings 패턴: display 가 빈 값이면 (CSS 기본인 table-row) 노출 상태,
        // 'none' 이면 숨김. 단 본 hidden <tr> 은 인라인 style="display:none" 으로
        // 시작하므로 첫 클릭은 빈 문자열로 켜고, 다음 클릭은 'none' 으로 끈다.
        if (row.style.display === "none") {
            row.style.display = "";
            wrap.setAttribute("aria-expanded", "true");
            wrap.classList.add("pg-wrap--expanded");
        } else {
            row.style.display = "none";
            wrap.setAttribute("aria-expanded", "false");
            wrap.classList.remove("pg-wrap--expanded");
        }
    }

    /**
     * .pg-wrap 위치 기준으로 툴팁을 viewport fixed 좌표에 배치한다.
     * 위 공간이 부족하면 아래로 자동 반전, 좌우는 viewport 8px 여백 클램핑.
     *
     * @param {HTMLElement} wrapEl    .pg-wrap 컨테이너
     * @param {HTMLElement} tooltipEl .pg-tooltip 요소
     */
    function showProgressTooltip(wrapEl, tooltipEl) {
        var rect = wrapEl.getBoundingClientRect();
        var viewportWidth = window.innerWidth;

        // 화면 밖에 미리 배치 후 크기 측정 (시각적 flicker 없음).
        tooltipEl.style.top = "-9999px";
        tooltipEl.style.left = "-9999px";
        tooltipEl.classList.add("pg-tooltip--visible");

        var tipWidth = tooltipEl.offsetWidth;
        var tipHeight = tooltipEl.offsetHeight;

        // 기본: 셀 위에 배치.
        var tipTop = rect.top - tipHeight - 8;
        var tipLeft = rect.left + rect.width / 2 - tipWidth / 2;

        // 위에 공간 부족 → 아래로 반전.
        if (tipTop < 8) {
            tipTop = rect.bottom + 8;
            tooltipEl.classList.add("pg-tooltip--below");
        } else {
            tooltipEl.classList.remove("pg-tooltip--below");
        }

        // 좌우 viewport 클램핑 (8px 여백).
        tipLeft = Math.max(8, Math.min(viewportWidth - tipWidth - 8, tipLeft));

        tooltipEl.style.top = tipTop + "px";
        tooltipEl.style.left = tipLeft + "px";
    }

    /**
     * 툴팁 숨김 + 방향 클래스 초기화.
     * @param {HTMLElement} tooltipEl .pg-tooltip 요소
     */
    function hideProgressTooltip(tooltipEl) {
        tooltipEl.classList.remove("pg-tooltip--visible", "pg-tooltip--below");
    }

    // ── 2. 상세 페이지 인라인 섹션 컨트롤 ─────────────────

    /**
     * 상세 페이지의 본인 조직 row 마다 [저장] [삭제] / 새 row 작성 버튼을 wire 한다.
     * 각 row 의 data-canonical-id, data-organization-id, (있으면) data-progress-id 만으로
     * URL 을 결정한다.
     */
    function initProgressDetailSection() {
        var section = document.querySelector(".pg-detail-section");
        if (!section) {
            return;
        }

        section.addEventListener("click", function (event) {
            var target = event.target;
            if (!(target instanceof HTMLElement)) {
                return;
            }

            // 새 row 작성 폼 토글 — '[ 우리 조직 입장 표명하기 ]' 버튼.
            if (target.classList.contains("pg-create-toggle-btn")) {
                event.preventDefault();
                handleCreateToggleClick(target);
                return;
            }

            var actionType = target.dataset.action;
            if (!actionType) {
                return;
            }

            var rowEl = target.closest(".pg-detail-row");
            if (!rowEl) {
                return;
            }

            event.preventDefault();
            if (actionType === "save") {
                handleSaveClick(rowEl);
            } else if (actionType === "create") {
                handleCreateSaveClick(rowEl, target);
            } else if (actionType === "delete") {
                handleDeleteClick(rowEl);
            } else if (actionType === "cancel") {
                handleCreateCancelClick(rowEl, target);
            }
        });
    }

    /**
     * '[ 우리 조직 입장 표명하기 ]' 버튼 클릭 — 인라인 폼 펼침/접기.
     * @param {HTMLElement} toggleBtn 클릭된 버튼
     */
    function handleCreateToggleClick(toggleBtn) {
        var rowEl = toggleBtn.closest(".pg-detail-row");
        if (!rowEl) {
            return;
        }
        var controls = rowEl.querySelector(".pg-detail-row__controls");
        if (!controls) {
            return;
        }
        var isHidden = controls.hasAttribute("hidden");
        if (isHidden) {
            controls.removeAttribute("hidden");
            toggleBtn.setAttribute("hidden", "");
        } else {
            controls.setAttribute("hidden", "");
            toggleBtn.removeAttribute("hidden");
        }
    }

    /**
     * 새 row 작성 [취소] — 폼을 접고 토글 버튼을 다시 노출.
     * @param {HTMLElement} rowEl
     * @param {HTMLElement} cancelBtn
     */
    function handleCreateCancelClick(rowEl, cancelBtn) {
        var controls = rowEl.querySelector(".pg-detail-row__controls");
        var toggle = rowEl.querySelector(".pg-create-toggle-btn");
        if (controls) {
            controls.setAttribute("hidden", "");
        }
        if (toggle) {
            toggle.removeAttribute("hidden");
        }
        clearRowFeedback(rowEl);
    }

    /**
     * 기존 row 의 [저장] 버튼 클릭 — PATCH 호출 후 reload.
     * @param {HTMLElement} rowEl
     */
    function handleSaveClick(rowEl) {
        var canonicalId = rowEl.dataset.canonicalId;
        var progressId = rowEl.dataset.progressId;
        if (!canonicalId || !progressId) {
            console.warn("[progress.js] 저장 실패 — canonical_id / progress_id 없음", rowEl);
            return;
        }
        var statusValue = rowEl.querySelector(".pg-status-select").value;
        var noteValue = rowEl.querySelector(".pg-note-textarea").value;
        clearRowFeedback(rowEl);
        sendJsonRequest("PATCH", "/canonical/" + canonicalId + "/progress/" + progressId, {
            status: statusValue,
            note: noteValue || null,
        }).then(function (result) {
            if (result.ok) {
                window.location.reload();
            } else {
                showRowFeedback(rowEl, result.message, "error");
            }
        });
    }

    /**
     * 새 row 작성 [저장] 버튼 클릭 — POST 호출 후 reload.
     * @param {HTMLElement} rowEl
     */
    function handleCreateSaveClick(rowEl) {
        var canonicalId = rowEl.dataset.canonicalId;
        var organizationId = rowEl.dataset.organizationId;
        if (!canonicalId || !organizationId) {
            console.warn("[progress.js] 새 row 저장 실패 — canonical_id / organization_id 없음", rowEl);
            return;
        }
        var statusValue = rowEl.querySelector(".pg-status-select").value;
        var noteValue = rowEl.querySelector(".pg-note-textarea").value;
        clearRowFeedback(rowEl);
        sendJsonRequest("POST", "/canonical/" + canonicalId + "/progress", {
            organization_id: parseInt(organizationId, 10),
            status: statusValue,
            note: noteValue || null,
        }).then(function (result) {
            if (result.ok) {
                window.location.reload();
            } else {
                showRowFeedback(rowEl, result.message, "error");
            }
        });
    }

    /**
     * [삭제] 버튼 클릭 — confirm 후 DELETE 호출.
     * @param {HTMLElement} rowEl
     */
    function handleDeleteClick(rowEl) {
        var canonicalId = rowEl.dataset.canonicalId;
        var progressId = rowEl.dataset.progressId;
        if (!canonicalId || !progressId) {
            return;
        }
        if (!window.confirm("이 조직의 진행 상태를 삭제하시겠습니까?")) {
            return;
        }
        clearRowFeedback(rowEl);
        sendJsonRequest(
            "DELETE",
            "/canonical/" + canonicalId + "/progress/" + progressId,
            null
        ).then(function (result) {
            if (result.ok) {
                window.location.reload();
            } else {
                showRowFeedback(rowEl, result.message, "error");
            }
        });
    }

    /**
     * fetch 래퍼 — JSON 요청·응답을 처리하고 ok/message 형태로 정규화한다.
     *
     * @param {string} method HTTP 메서드.
     * @param {string} url    요청 URL.
     * @param {object|null} body JSON 본문 (없으면 null).
     * @returns {Promise<{ok: boolean, message: string}>}
     */
    function sendJsonRequest(method, url, body) {
        var fetchOptions = {
            method: method,
            headers: { "Content-Type": "application/json" },
            credentials: "same-origin",
        };
        if (body !== null) {
            fetchOptions.body = JSON.stringify(body);
        }
        return fetch(url, fetchOptions)
            .then(function (response) {
                return response
                    .json()
                    .catch(function () {
                        return null;
                    })
                    .then(function (payload) {
                        if (response.ok) {
                            return { ok: true, message: "" };
                        }
                        var detailMessage =
                            (payload && payload.detail) ||
                            "요청을 처리하지 못했습니다 (HTTP " + response.status + ").";
                        return { ok: false, message: detailMessage };
                    });
            })
            .catch(function (networkError) {
                console.error("[progress.js] 네트워크 오류", networkError);
                return { ok: false, message: "네트워크 오류로 요청을 보낼 수 없습니다." };
            });
    }

    /**
     * row 안에 feedback 박스를 띄운다 (선점 충돌 / 권한 거부 / 네트워크 오류 등).
     * 이미 박스가 있으면 메시지를 갈아끼운다.
     *
     * @param {HTMLElement} rowEl
     * @param {string} message 사용자에게 보여줄 한글 메시지.
     * @param {"error"|"info"} variant 색상 분기.
     */
    function showRowFeedback(rowEl, message, variant) {
        var feedback = rowEl.querySelector(".pg-detail-row__feedback");
        if (!feedback) {
            feedback = document.createElement("p");
            feedback.className = "pg-detail-row__feedback";
            rowEl.appendChild(feedback);
        }
        feedback.classList.toggle("pg-detail-row__feedback--info", variant === "info");
        feedback.textContent = message;
        feedback.removeAttribute("hidden");
    }

    /**
     * row 의 feedback 박스를 숨긴다 (재시도 시 깜빡임 방지).
     * @param {HTMLElement} rowEl
     */
    function clearRowFeedback(rowEl) {
        var feedback = rowEl.querySelector(".pg-detail-row__feedback");
        if (feedback) {
            feedback.setAttribute("hidden", "");
            feedback.textContent = "";
        }
    }

    // ── 3. 부트스트랩 ──────────────────────────────────────

    /**
     * DOM 로드 후 초기화 — 목록 / 상세 양쪽에서 그대로 동작.
     * 서로 매칭되는 셀 / 섹션이 없으면 early-return 으로 무해.
     */
    function bootstrap() {
        initProgressTooltips();
        initProgressDetailSection();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", bootstrap);
    } else {
        bootstrap();
    }
})();
