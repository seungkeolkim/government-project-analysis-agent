// 관련성 판정 모달/배지 인터랙션 (Phase 3a / 00035-3)
// Jinja2 템플릿 변수 없이 순수 JS 로만 동작한다.
// 모달이 없는 페이지(비로그인, 관리자 페이지 등)에서는 초기화 직후 반환한다.
(function () {
    'use strict';

    var modal = document.getElementById('relevance-modal');
    if (!modal) { return; }

    var form = document.getElementById('relevance-form');
    var reasonTextarea = document.getElementById('modal-reason');
    var deleteBtn = document.getElementById('relevance-delete-btn');
    var cancelBtn = document.getElementById('relevance-cancel-btn');
    var errorMsg = document.getElementById('modal-error-msg');

    // 현재 열려 있는 배지의 .rj-wrap 요소를 저장한다.
    var currentWrap = null;

    // 배지 클릭 시 외부에서 호출되는 진입점.
    // wrapEl: .rj-wrap 요소 (data-canonical-id, data-my-verdict, data-my-reason 속성 보유).
    window.openRelevanceModal = function (wrapEl) {
        currentWrap = wrapEl;
        var verdict = wrapEl.dataset.myVerdict || '';
        var reason = wrapEl.dataset.myReason || '';

        // 라디오 초기화
        form.querySelectorAll('input[name="verdict"]').forEach(function (radio) {
            radio.checked = (radio.value === verdict);
        });
        // 사유 초기화
        reasonTextarea.value = reason;
        // 삭제 버튼: 기존 판정이 있을 때만 표시
        deleteBtn.style.display = verdict ? '' : 'none';
        // 에러 초기화
        errorMsg.textContent = '';

        modal.showModal();
    };

    // 저장 핸들러 — POST /canonical/{id}/relevance
    form.addEventListener('submit', function (e) {
        e.preventDefault();
        if (!currentWrap) { return; }

        var canonicalId = currentWrap.dataset.canonicalId;
        var selectedVerdict = '';
        form.querySelectorAll('input[name="verdict"]').forEach(function (radio) {
            if (radio.checked) { selectedVerdict = radio.value; }
        });

        if (!selectedVerdict) {
            errorMsg.textContent = '관련/무관을 선택해 주세요.';
            return;
        }

        var reason = reasonTextarea.value.trim();

        fetch('/canonical/' + canonicalId + '/relevance', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ verdict: selectedVerdict, reason: reason || null }),
        })
        .then(function (resp) {
            if (!resp.ok) {
                return resp.json().catch(function () { return {}; }).then(function (data) {
                    errorMsg.textContent = data.detail || '저장 실패. 다시 시도해 주세요.';
                });
            }
            updateBadge(currentWrap, selectedVerdict, reason);
            modal.close();
        })
        .catch(function () {
            errorMsg.textContent = '네트워크 오류. 다시 시도해 주세요.';
        });
    });

    // 판정 취소 핸들러 — DELETE /canonical/{id}/relevance
    deleteBtn.addEventListener('click', function () {
        if (!currentWrap) { return; }
        var canonicalId = currentWrap.dataset.canonicalId;

        fetch('/canonical/' + canonicalId + '/relevance', {
            method: 'DELETE',
        })
        .then(function (resp) {
            if (!resp.ok) {
                return resp.json().catch(function () { return {}; }).then(function (data) {
                    errorMsg.textContent = data.detail || '삭제 실패. 다시 시도해 주세요.';
                });
            }
            updateBadge(currentWrap, '', '');
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

    // 배지 DOM 인플레이스 갱신 — 페이지 리로드 없이 verdict/reason 반영.
    function updateBadge(wrapEl, verdict, reason) {
        wrapEl.dataset.myVerdict = verdict;
        wrapEl.dataset.myReason = reason || '';

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
}());
