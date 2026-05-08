// 관련성 판정 모달/배지 인터랙션 (Phase 3a / 00035-3 → task 00085 조직 단위 확장).
// Jinja2 템플릿 변수 없이 순수 JS 로만 동작한다. 모든 데이터는 DOM data-* 속성으로 전달된다.
//
// task 00085 변경 사항:
//   - 모달에 '판정 주체' 라디오 (개인/조직) + 조직 드롭다운 추가.
//   - openRelevanceModal 이 호출자(배지 또는 detail-page 행 버튼) 의 data-* 를 읽어
//     라디오/드롭다운/verdict/reason 4 필드를 prefill 한다.
//   - POST/DELETE 요청 body 에 organization_id 가 함께 전송된다 (개인은 null).
//   - 상세 페이지 .rj-detail-edit-btn / .rj-detail-create-btn / .rj-detail-delete-btn
//     클릭 시 모달을 같은 흐름으로 띄운다 + 단독 삭제는 별도 confirm 후 DELETE.
//
// 모달이 없는 페이지(비로그인, 관리자 페이지 등)에서는 초기화 직후 반환한다.
(function () {
    'use strict';

    // 툴팁 hover 는 로그인 여부 무관하게 항상 초기화한다.
    initTooltips();

    var modal = document.getElementById('relevance-modal');
    if (!modal) {
        // 비로그인 페이지에서도 detail 페이지의 .rj-detail-delete-btn 은 존재할 수 있다.
        // 모달이 없으면 어차피 작성/수정 흐름이 막혀 있으니 여기서 종료해도 안전하다.
        // (detail-page 의 본인 영역 자체가 {% if current_user %} 로 가려져 있다.)
        return;
    }

    var form = document.getElementById('relevance-form');
    var reasonTextarea = document.getElementById('modal-reason');
    var cancelBtn = document.getElementById('relevance-cancel-btn');
    var errorMsg = document.getElementById('modal-error-msg');
    // 판정 주체 라디오 그룹 + 조직 드롭다운 행 (없을 수 있음 — 무소속).
    var subjectRadios = form.querySelectorAll('input[name="rj-subject"]');
    var organizationRow = document.getElementById('rj-modal-organization-row');
    var organizationSelect = document.getElementById('rj-modal-organization');
    // task 00089 — 내 판정 목록 컨테이너.
    var myListContainer = document.getElementById('rj-modal-mine-list');

    // 현재 열려 있는 호출자 (.rj-wrap 또는 detail-page 의 버튼) 를 저장한다.
    // 호출자의 data-* 가 폼 prefill 의 출처이며, 저장 성공 시 거기 화면도 갱신한다.
    var currentInvoker = null;

    /**
     * 모달의 '판정 주체' 라디오 + 조직 드롭다운을 현재 라디오 선택에 맞춰 동기화한다.
     *
     * - 개인 선택: 조직 드롭다운 행 숨김.
     * - 조직 선택 (단일 소속): 행은 보이지만 select 가 없으므로 hidden field 도 없고,
     *   submit 시 data-single-organization-id 로 organization_id 를 채운다.
     * - 조직 선택 (복수 소속): 드롭다운 행 표시, select 의 값이 organization_id.
     */
    function syncSubjectVisibility() {
        var subject = getCheckedSubject();
        if (!organizationRow) {
            // 무소속 사용자 — 조직 라디오 자체 disabled. 항상 개인 모드.
            return;
        }
        if (subject === 'organization') {
            organizationRow.style.display = '';
        } else {
            organizationRow.style.display = 'none';
        }
    }

    /**
     * 현재 모달 폼에서 선택된 '판정 주체' 라디오 값을 반환한다.
     * 'personal' | 'organization' (무소속 사용자는 항상 'personal').
     */
    function getCheckedSubject() {
        for (var i = 0; i < subjectRadios.length; i++) {
            if (subjectRadios[i].checked) {
                return subjectRadios[i].value;
            }
        }
        return 'personal';
    }

    /**
     * 모달 폼에서 결정된 organization_id 를 추출한다.
     *
     * - 개인 라디오 선택 → null
     * - 조직 라디오 선택 + 단일 조직 모드 → data-single-organization-id (정수)
     * - 조직 라디오 선택 + 복수 조직 모드 → select 의 현재 값 (정수)
     * - 조직 라디오 선택 + organizationRow 없음 → null (이론상 불가, 안전 차단)
     */
    function resolveOrganizationId() {
        if (getCheckedSubject() !== 'organization') {
            return null;
        }
        if (!organizationRow) {
            return null;
        }
        var mode = organizationRow.dataset.modalOrgMode;
        if (mode === 'single') {
            var singleIdRaw = organizationRow.dataset.singleOrganizationId;
            return singleIdRaw ? parseInt(singleIdRaw, 10) : null;
        }
        // 'multiple'
        if (organizationSelect && organizationSelect.value) {
            return parseInt(organizationSelect.value, 10);
        }
        return null;
    }

    // 라디오 / 드롭다운 변경 시 표시 토글.
    Array.prototype.forEach.call(subjectRadios, function (radio) {
        radio.addEventListener('change', syncSubjectVisibility);
    });

    /**
     * 외부에서 모달을 여는 진입점.
     *
     * @param {HTMLElement} invokerEl
     *     모달을 띄우는 트리거 요소. data-canonical-id, data-my-verdict, data-my-reason,
     *     data-my-origin (personal | organization | none), data-my-organization-id 를
     *     통해 폼이 prefill 된다.
     *     - 목록의 .rj-wrap (배지 컨테이너) — 본인 큰 배지 row 의 verdict 로 prefill.
     *     - 상세 페이지의 .rj-detail-edit-btn — 그 행의 (개인 또는 특정 조직) row 로 prefill.
     *     - 상세 페이지의 .rj-detail-create-btn — 빈 폼 (개인 라디오 + verdict 미선택).
     */
    window.openRelevanceModal = function (invokerEl) {
        currentInvoker = invokerEl;
        var verdict = invokerEl.dataset.myVerdict || '';
        var reason = invokerEl.dataset.myReason || '';
        var origin = invokerEl.dataset.myOrigin || 'none';
        var organizationIdRaw = invokerEl.dataset.myOrganizationId || '';

        // 판정 주체 라디오 prefill — origin 이 'organization' 이면 조직 라디오, 그 외엔 개인.
        Array.prototype.forEach.call(subjectRadios, function (radio) {
            if (origin === 'organization' && radio.value === 'organization' && !radio.disabled) {
                radio.checked = true;
            } else if (radio.value === 'personal') {
                radio.checked = (origin !== 'organization' || areAllOrganizationRadiosDisabled());
            } else {
                radio.checked = false;
            }
        });

        // 복수 조직 드롭다운 prefill — origin=organization 이고 organization_id 가 들어왔을 때.
        if (organizationSelect && origin === 'organization' && organizationIdRaw) {
            organizationSelect.value = String(parseInt(organizationIdRaw, 10));
        }

        // verdict 라디오 prefill
        form.querySelectorAll('input[name="verdict"]').forEach(function (radio) {
            radio.checked = (radio.value === verdict);
        });
        // 사유 prefill
        reasonTextarea.value = reason;
        // 에러 초기화
        errorMsg.textContent = '';

        syncSubjectVisibility();

        // task 00089 — 모달 열릴 때마다 본인 판정 목록 새로고침.
        var canonicalId = invokerEl.dataset.canonicalId;
        loadMyJudgmentsList(canonicalId);

        modal.showModal();
    };

    /**
     * 조직 라디오가 모두 disabled (무소속 사용자) 인지 검사한다.
     * 무소속이면 origin=organization 으로 호출돼도 개인으로 fallback 한다.
     */
    function areAllOrganizationRadiosDisabled() {
        for (var i = 0; i < subjectRadios.length; i++) {
            if (subjectRadios[i].value === 'organization' && !subjectRadios[i].disabled) {
                return false;
            }
        }
        return true;
    }

    // 저장 핸들러 — POST /canonical/{id}/relevance
    form.addEventListener('submit', function (e) {
        e.preventDefault();
        if (!currentInvoker) { return; }

        var canonicalId = currentInvoker.dataset.canonicalId;
        var selectedVerdict = '';
        form.querySelectorAll('input[name="verdict"]').forEach(function (radio) {
            if (radio.checked) { selectedVerdict = radio.value; }
        });

        if (!selectedVerdict) {
            errorMsg.textContent = '관련/무관을 선택해 주세요.';
            return;
        }

        var subject = getCheckedSubject();
        var organizationIdValue = resolveOrganizationId();
        if (subject === 'organization' && organizationIdValue === null) {
            errorMsg.textContent = '소속 조직을 선택해 주세요.';
            return;
        }

        var reason = reasonTextarea.value.trim();
        var requestBody = {
            verdict: selectedVerdict,
            reason: reason || null,
            organization_id: organizationIdValue,
        };

        fetch('/canonical/' + canonicalId + '/relevance', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(requestBody),
        })
        .then(function (resp) {
            if (!resp.ok) {
                return resp.json().catch(function () { return {}; }).then(function (data) {
                    errorMsg.textContent = data.detail || '저장 실패. 다시 시도해 주세요.';
                });
            }
            // 성공 시: 호출자가 .rj-wrap (목록 배지) 이면 인플레이스 갱신,
            // 상세 페이지 버튼이면 페이지 새로고침으로 풀어 표시 영역을 다시 그린다.
            if (currentInvoker.classList && currentInvoker.classList.contains('rj-wrap')) {
                updateBadgeWrap(currentInvoker, selectedVerdict, reason, subject, organizationIdValue);
            } else {
                window.location.reload();
            }
            modal.close();
        })
        .catch(function () {
            errorMsg.textContent = '네트워크 오류. 다시 시도해 주세요.';
        });
    });

    // 닫기 버튼
    cancelBtn.addEventListener('click', function () {
        modal.close();
    });

    // dialog 배경 클릭 → 닫기
    modal.addEventListener('click', function (e) {
        if (e.target === modal) { modal.close(); }
    });

    /**
     * DELETE /canonical/{id}/relevance 호출. 성공 시 onSuccess 콜백 실행.
     * organizationIdValue 는 null (개인) 또는 정수.
     */
    function sendDeleteRequest(canonicalId, organizationIdValue, onSuccess) {
        fetch('/canonical/' + canonicalId + '/relevance', {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ organization_id: organizationIdValue }),
        })
        .then(function (resp) {
            if (!resp.ok) {
                return resp.json().catch(function () { return {}; }).then(function (data) {
                    errorMsg.textContent = data.detail || '삭제 실패. 다시 시도해 주세요.';
                });
            }
            onSuccess();
        })
        .catch(function () {
            errorMsg.textContent = '네트워크 오류. 다시 시도해 주세요.';
        });
    }

    /**
     * GET /canonical/{id}/relevance/mine 로 본인 판정 목록을 가져와 렌더링한다.
     *
     * @param {string} canonicalId   canonical_project PK (문자열)
     * @param {Function} [onRendered] 렌더 완료 후 items 배열을 인수로 호출하는 콜백 (선택)
     */
    function loadMyJudgmentsList(canonicalId, onRendered) {
        if (!myListContainer) { return; }
        // 로딩 중 표시
        myListContainer.innerHTML = '';
        var loadingLi = document.createElement('li');
        loadingLi.className = 'rj-modal__mine-empty';
        loadingLi.textContent = '불러오는 중…';
        myListContainer.appendChild(loadingLi);

        fetch('/canonical/' + canonicalId + '/relevance/mine')
        .then(function (resp) {
            if (!resp.ok) {
                throw new Error('fetch failed: ' + resp.status);
            }
            return resp.json();
        })
        .then(function (data) {
            var items = (data && data.items) ? data.items : [];
            renderMyJudgmentsList(items, canonicalId);
            if (onRendered) { onRendered(items); }
        })
        .catch(function () {
            if (myListContainer) {
                myListContainer.innerHTML = '';
                var errLi = document.createElement('li');
                errLi.className = 'rj-modal__mine-empty';
                errLi.textContent = '목록을 불러올 수 없습니다.';
                myListContainer.appendChild(errLi);
            }
        });
    }

    /**
     * 본인 판정 items 배열을 myListContainer 에 DOM 으로 렌더링한다.
     * XSS 방지를 위해 textContent / DOM 생성 방식을 사용한다.
     *
     * @param {Array}  items        GET /relevance/mine 응답의 items 배열
     * @param {string} canonicalId  DELETE 요청에 쓸 canonical_project PK
     */
    function renderMyJudgmentsList(items, canonicalId) {
        if (!myListContainer) { return; }
        myListContainer.innerHTML = '';

        if (items.length === 0) {
            var emptyLi = document.createElement('li');
            emptyLi.className = 'rj-modal__mine-empty';
            emptyLi.textContent = '등록된 판정이 없습니다.';
            myListContainer.appendChild(emptyLi);
            return;
        }

        items.forEach(function (item) {
            var li = document.createElement('li');
            li.className = 'rj-modal__mine-item';

            // 판정 주체 (개인 또는 조직명)
            var subjectSpan = document.createElement('span');
            subjectSpan.className = 'rj-modal__mine-subject';
            subjectSpan.textContent = item.organization_name || '개인';

            // verdict 배지
            var verdictSpan = document.createElement('span');
            verdictSpan.className = 'rj-modal__mine-verdict rj-modal__mine-verdict--' +
                (item.verdict === '관련' ? 'related' : 'unrelated');
            verdictSpan.textContent = item.verdict;

            // 작성 시점 — ISO8601 → 사용자 친화 포맷
            var dateSpan = document.createElement('span');
            dateSpan.className = 'rj-modal__mine-date';
            dateSpan.textContent = item.decided_at
                ? new Date(item.decided_at).toLocaleString('ko-KR')
                : '';

            // X 삭제 버튼 — 클로저로 organization_id 캡처
            var delBtn = document.createElement('button');
            delBtn.type = 'button';
            delBtn.className = 'rj-modal__mine-delete-btn';
            delBtn.setAttribute('aria-label', '판정 삭제');
            delBtn.textContent = '✕';
            (function (orgId) {
                delBtn.addEventListener('click', function () {
                    handleDeleteMineItem(canonicalId, orgId);
                });
            }(item.organization_id));

            li.appendChild(subjectSpan);
            li.appendChild(verdictSpan);
            li.appendChild(dateSpan);
            li.appendChild(delBtn);
            myListContainer.appendChild(li);
        });
    }

    /**
     * 내 판정 목록의 X 버튼 핸들러.
     * DELETE /canonical/{id}/relevance 후 목록을 재갱신하고 배지도 인플레이스 갱신한다.
     *
     * @param {string}   canonicalId      canonical_project PK
     * @param {number|null} organizationId 삭제 대상 organization_id (개인이면 null)
     */
    function handleDeleteMineItem(canonicalId, organizationId) {
        fetch('/canonical/' + canonicalId + '/relevance', {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ organization_id: organizationId }),
        })
        .then(function (resp) {
            if (!resp.ok) {
                return resp.json().catch(function () { return {}; }).then(function (data) {
                    alert(data.detail || '삭제 실패. 다시 시도해 주세요.');
                });
            }
            // 삭제 성공 → 목록 재갱신 후 배지 인플레이스 갱신
            loadMyJudgmentsList(canonicalId, function (items) {
                if (currentInvoker && currentInvoker.classList && currentInvoker.classList.contains('rj-wrap')) {
                    if (items.length === 0) {
                        updateBadgeWrap(currentInvoker, '', '', 'none', null);
                    } else {
                        // 첫 항목(개인 판정 우선)으로 배지 갱신
                        var first = items[0];
                        var subj = first.organization_id ? 'organization' : 'personal';
                        updateBadgeWrap(currentInvoker, first.verdict, first.reason || '', subj, first.organization_id !== undefined ? first.organization_id : null);
                    }
                }
            });
        })
        .catch(function () {
            alert('네트워크 오류. 다시 시도해 주세요.');
        });
    }

    /**
     * 목록 배지 (.rj-wrap) 의 큰 배지 DOM 을 페이지 리로드 없이 인플레이스 갱신한다.
     * - subject='personal' 이면 개인 row 가 됐다 — verdict 갱신 + organization_id 비움.
     * - subject='organization' 이면 본인 조직 row 가 새로 생기거나 갱신됐다 — 카운터·툴팁
     *   재계산은 정확히 못 하므로 큰 배지 verdict 만 반영하고, 정확한 표시는 다음 새로고침에 의존.
     * - verdict='' 은 삭제 — 개인 슬롯 또는 해당 조직 슬롯을 미검토로 되돌린다.
     */
    function updateBadgeWrap(wrapEl, verdict, reason, subject, organizationIdValue) {
        wrapEl.dataset.myVerdict = verdict;
        wrapEl.dataset.myReason = reason || '';
        if (verdict) {
            wrapEl.dataset.myOrigin = (subject === 'organization') ? 'organization' : 'personal';
            wrapEl.dataset.myOrganizationId = (organizationIdValue !== null && organizationIdValue !== undefined) ? String(organizationIdValue) : '';
        } else {
            // 삭제 — origin 도 none 으로 되돌리고 organization_id 도 비움.
            wrapEl.dataset.myOrigin = 'none';
            wrapEl.dataset.myOrganizationId = '';
        }

        var badge = wrapEl.querySelector('.rj-badge');
        if (!badge) { return; }
        badge.classList.remove('rj-badge--related', 'rj-badge--unrelated', 'rj-badge--unreviewed');
        if (verdict === '관련') {
            badge.classList.add('rj-badge--related');
            badge.textContent = '관련';
        } else if (verdict === '무관') {
            badge.classList.add('rj-badge--unrelated');
            badge.textContent = '무관';
        } else {
            badge.classList.add('rj-badge--unreviewed');
            badge.textContent = '미검토';
        }
    }

    /**
     * 상세 페이지의 본인 row 옆 [수정] / [새 판정 추가] 버튼 클릭 시 모달을 띄운다.
     * 버튼 자체에 data-* 가 붙어 있으므로 해당 버튼을 invoker 로 그대로 넘긴다.
     */
    document.querySelectorAll('.rj-detail-edit-btn, .rj-detail-create-btn').forEach(function (btn) {
        btn.addEventListener('click', function (e) {
            e.preventDefault();
            window.openRelevanceModal(btn);
        });
    });

    /**
     * 상세 페이지의 본인 row 옆 [삭제] 버튼 — 모달 거치지 않고 즉시 confirm 후 DELETE.
     * data-canonical-id / data-my-organization-id 만으로 트리플이 결정된다.
     */
    document.querySelectorAll('.rj-detail-delete-btn').forEach(function (btn) {
        btn.addEventListener('click', function (e) {
            e.preventDefault();
            var canonicalId = btn.dataset.canonicalId;
            var organizationIdRaw = btn.dataset.myOrganizationId;
            var organizationIdValue = organizationIdRaw ? parseInt(organizationIdRaw, 10) : null;
            if (!window.confirm('이 판정을 삭제하시겠습니까?')) { return; }
            currentInvoker = btn;
            sendDeleteRequest(canonicalId, organizationIdValue, function () {
                window.location.reload();
            });
        });
    });

    /**
     * 각 .rj-wrap 에 mouseenter/mouseleave 와 focusin/focusout 이벤트를 직접 등록하여
     * position: fixed 기준의 툴팁을 viewport 좌표로 배치한다.
     * 로그인 여부 무관하게 실행되므로 IIFE 최상단에서 호출한다.
     */
    function initTooltips() {
        document.querySelectorAll('.rj-wrap').forEach(function (wrap) {
            var tooltip = wrap.querySelector('.rj-tooltip');
            if (!tooltip) { return; }

            wrap.addEventListener('mouseenter', function () {
                showTooltip(wrap, tooltip);
            });
            wrap.addEventListener('mouseleave', function () {
                hideTooltip(tooltip);
            });
            // 키보드 사용자를 위한 접근성 지원 (.rj-badge 가 button 이므로 자연스럽게 focus 가능)
            wrap.addEventListener('focusin', function () {
                showTooltip(wrap, tooltip);
            });
            wrap.addEventListener('focusout', function () {
                hideTooltip(tooltip);
            });
        });
    }

    /**
     * 툴팁을 viewport 기준 fixed 좌표로 배치하고 표시한다.
     * 배지 위에 배치하되, 위 공간이 부족하면 아래로 자동 반전한다.
     * 좌우는 viewport 경계(8px 여백)에 클램핑한다.
     *
     * @param {HTMLElement} wrapEl    .rj-wrap 컨테이너
     * @param {HTMLElement} tooltipEl .rj-tooltip 요소
     */
    function showTooltip(wrapEl, tooltipEl) {
        var rect = wrapEl.getBoundingClientRect();
        var vpW = window.innerWidth;

        // 화면 밖에 배치 후 크기 측정 (렌더링 직전 reflow 강제, 시각적 flicker 없음)
        tooltipEl.style.top = '-9999px';
        tooltipEl.style.left = '-9999px';
        tooltipEl.classList.add('rj-tooltip--visible');

        var tipW = tooltipEl.offsetWidth;
        var tipH = tooltipEl.offsetHeight;

        // 기본: 배지 위에 배치 (아래 화살표)
        var tipTop = rect.top - tipH - 8;
        var tipLeft = rect.left + rect.width / 2 - tipW / 2;

        // 위에 공간 부족(8px 이상 필요) → 아래로 반전 (위 화살표)
        if (tipTop < 8) {
            tipTop = rect.bottom + 8;
            tooltipEl.classList.add('rj-tooltip--below');
        } else {
            tooltipEl.classList.remove('rj-tooltip--below');
        }

        // 좌우 뷰포트 경계 클램핑 (8px 여백)
        tipLeft = Math.max(8, Math.min(vpW - tipW - 8, tipLeft));

        tooltipEl.style.top = tipTop + 'px';
        tooltipEl.style.left = tipLeft + 'px';
    }

    /**
     * 툴팁을 숨기고 방향 클래스를 초기화한다.
     *
     * @param {HTMLElement} tooltipEl .rj-tooltip 요소
     */
    function hideTooltip(tooltipEl) {
        tooltipEl.classList.remove('rj-tooltip--visible', 'rj-tooltip--below');
    }
}());
