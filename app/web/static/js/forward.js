// 공고 포워딩("메일로 보내기") 모달 인터랙션 (Phase A-2 Part 2 / task 00109-8).
//
// relevance.js / favorites.js 와 동일하게 단일 IIFE 로 동작한다. base.html 이
// 모든 페이지에서 이 스크립트를 로드하지만, #forward-modal 이 없는 페이지
// (비로그인, 관리자 페이지 등) 에서는 초기화 직후 early-return 한다.
//
// 책임 범위 (00109-8 — 발송 모달 골격):
//   - "메일로 보내기" 버튼 클릭 → 모달 열기 + 제목 prefill + 필드 초기화.
//   - 추가 메시지 글자 수 카운터.
//   - 본문 미리보기 toggle + iframe 클라이언트 mock 렌더.
//   - 발송 버튼: 입력 검증 → POST /api/canonical/{id}/forward → spinner + 모달
//     잠금 → 결과 박스(성공 초록 / 실패 빨강) → 성공 시 3 초 후 자동 닫기 +
//     발송 이력 새로고침 트리거.
//   - 취소 버튼 / 배경 클릭 → 모달 닫기 (입력 미유지).
//
// 다른 subtask 와의 계약 지점:
//   - collectRecipients(): #forward-recipients-chips 안의
//     .forward-chip[data-email] 을 읽어 수신자 이메일 목록을 만든다. chip 입력 /
//     자동완성 UI 는 00109-9 가 이 컨테이너 위에 구현한다 — 그 전까지는 chip 이
//     없어 발송 검증이 "받는 사람 1 명 이상" 에서 막힌다(정상 동작).
//   - window.refreshForwardHistory: 발송 성공 후 호출한다. 발송 이력 섹션은
//     00109-10 이 구현하며 이 함수를 window 에 정의한다. 아직 없으면 무해하게
//     건너뛴다.
(function () {
    'use strict';

    var modal = document.getElementById('forward-modal');
    if (!modal) {
        // 비로그인 페이지 / 모달이 렌더되지 않는 페이지 — 안전하게 종료한다.
        return;
    }

    var form = document.getElementById('forward-form');
    var subjectInput = document.getElementById('forward-subject');
    var messageTextarea = document.getElementById('forward-message');
    var messageCount = document.getElementById('forward-message-count');
    var organizationRow = document.getElementById('forward-organization-row');
    var organizationSelect = document.getElementById('forward-organization');
    var previewToggle = document.getElementById('forward-preview-toggle');
    var previewRegion = document.getElementById('forward-preview-region');
    var previewFrame = document.getElementById('forward-preview-frame');
    var chipsContainer = document.getElementById('forward-recipients-chips');
    var recipientsBox = document.getElementById('forward-recipients');
    var recipientInput = document.getElementById('forward-recipient-input');
    var autocompleteBox = document.getElementById('forward-autocomplete');
    var errorMsg = document.getElementById('forward-error-msg');
    var resultBox = document.getElementById('forward-result-box');
    var sendBtn = document.getElementById('forward-send-btn');
    var spinner = document.getElementById('forward-spinner');
    var cancelBtn = document.getElementById('forward-cancel-btn');
    // task 00120 — 발송 진행 영역 (프로그레스 바 + N명 중 M명 카운터).
    var progressBox = document.getElementById('forward-progress');
    var progressBar = document.getElementById('forward-progress-bar');
    var progressText = document.getElementById('forward-progress-text');

    // 서버 측 제한과 일치시킨 클라이언트 검증 상수 (routes/forward.py).
    var SUBJECT_MAX_LENGTH = 200;
    var MESSAGE_MAX_LENGTH = 5000;
    var RECIPIENTS_MAX_COUNT = 50;
    // 모달 진입 시 제목 input 에 채울 default 제목 (build_default_forward_subject
    // 와 동일 포맷 — 제목 100 자 초과 시 truncate).
    var SUBJECT_PREFIX = '[정부사업 모니터링] 공고 검토 요청: ';
    var TITLE_TRUNCATE_LENGTH = 100;
    // 발송 성공 후 모달 자동 닫기까지의 지연.
    var AUTO_CLOSE_DELAY_MS = 3000;
    // 자동완성 debounce — 사용자가 입력을 멈춘 뒤 검색 API 를 호출하기까지의 지연
    // (design note §9-l 확정값).
    var AUTOCOMPLETE_DEBOUNCE_MS = 250;
    // /api/users/search 로 한 번에 받아올 최대 결과 수 (서버 default 와 동일).
    var USER_SEARCH_LIMIT = 10;
    // 자동완성 검색어 길이 상한 — 서버 q 파라미터 제한(1~50자)과 일치시킨다.
    var USER_SEARCH_QUERY_MAX_LENGTH = 50;
    // 외부 이메일 직접 입력 시 chip 으로 확정하기 위한 간단 형식 검증 정규식
    // (design note §9-l 확정값).
    var EMAIL_PATTERN = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
    // task 00120 — polling 간격 (1초). plan 의 strategy_note 와 일치.
    var POLLING_INTERVAL_MS = 1000;
    // task 00120 — polling 도중 HTTP 오류가 연속으로 발생해도 일시적 네트워크
    // 끊김을 흡수하기 위한 임계치. 이 횟수를 넘으면 polling 중단 + 에러 박스.
    var POLLING_MAX_CONSECUTIVE_FAILURES = 3;

    // 현재 모달이 대상으로 하는 canonical_project PK (문자열).
    var currentCanonicalId = null;
    // 발송 성공 후 자동 닫기 타이머 핸들 — 닫힐 때 정리한다.
    var autoCloseTimerId = null;
    // 발송 진행 중 모달 잠금 여부 — 잠겨 있으면 배경 클릭/취소로 닫지 않는다.
    var isLocked = false;
    // 자동완성 debounce 타이머 핸들 — 새 입력마다 정리하고 다시 건다.
    var autocompleteTimerId = null;
    // 자동완성 요청 토큰 — 응답이 도착했을 때 가장 최근 요청인지 판별해 stale
    // 응답으로 드롭다운을 덮어쓰지 않게 한다.
    var autocompleteRequestToken = 0;
    // 키보드(↑/↓)로 강조 중인 드롭다운 항목 index. -1 이면 강조 없음.
    var autocompleteActiveIndex = -1;
    // task 00120 — polling 상태. 타이머 핸들 / 폴링 대상 canonical 및 forward_log
    // id / 총 수신자 수 / 연속 실패 카운터. forward_log_id 는 응답이 도착했을 때
    // 가장 최근 polling 인지 확인해 stale 응답으로 막대를 덮어쓰지 않는 용도로도
    // 쓴다. 모달 닫힐 때 stopProgressPolling 으로 일괄 정리한다.
    var progressPollingTimerId = null;
    var progressPollingCanonicalId = null;
    var progressPollingForwardLogId = null;
    var progressPollingTotalRecipients = 0;
    var progressPollingConsecutiveFailures = 0;

    // ──────────────────────────────────────────────────────────
    // 헬퍼
    // ──────────────────────────────────────────────────────────

    /**
     * 문자열을 HTML 텍스트로 안전하게 이스케이프한다.
     * 미리보기 iframe srcdoc 조립 시 사용자 입력(제목/메시지/메타)을 그대로
     * 끼워 넣지 않도록 textContent → innerHTML 변환으로 처리한다.
     *
     * @param {*} text 이스케이프할 값 (null/undefined 는 빈 문자열로 처리).
     * @returns {string} HTML 에 안전하게 삽입 가능한 문자열.
     */
    function escapeHtml(text) {
        var holder = document.createElement('div');
        holder.textContent = (text === null || text === undefined) ? '' : String(text);
        return holder.innerHTML;
    }

    /**
     * 공고 제목으로 default 메일 제목을 만든다.
     * 서버의 build_default_forward_subject 와 동일하게, 제목이 100 자를 넘으면
     * 100 자까지 자르고 '…' 를 붙인다.
     *
     * @param {string} title 공고 제목.
     * @returns {string} '[정부사업 모니터링] 공고 검토 요청: {title}' 형식 제목.
     */
    function buildDefaultSubject(title) {
        var trimmedTitle = title || '';
        if (trimmedTitle.length > TITLE_TRUNCATE_LENGTH) {
            trimmedTitle = trimmedTitle.slice(0, TITLE_TRUNCATE_LENGTH) + '…';
        }
        return SUBJECT_PREFIX + trimmedTitle;
    }

    /**
     * 발신 조직 row 의 data-* 에서 최종 sender_organization_id 를 추출한다.
     *
     * - 'single' 모드  → data-single-organization-id (정수).
     * - 'multiple' 모드 → <select> 의 현재 값 (정수).
     * - 'none' 모드(무소속) → null.
     *
     * @returns {number|null} sender_organization_id 또는 null.
     */
    function resolveOrganizationId() {
        if (!organizationRow) {
            return null;
        }
        var mode = organizationRow.dataset.forwardOrgMode;
        if (mode === 'single') {
            var singleIdRaw = organizationRow.dataset.singleOrganizationId;
            return singleIdRaw ? parseInt(singleIdRaw, 10) : null;
        }
        if (mode === 'multiple' && organizationSelect && organizationSelect.value) {
            return parseInt(organizationSelect.value, 10);
        }
        return null;
    }

    /**
     * 수신자 chip 목록에서 이메일 주소 배열을 수집한다.
     *
     * chip DOM(.forward-chip[data-email])은 00109-9 가 #forward-recipients-chips
     * 컨테이너 안에 채운다. 본 함수가 00109-8 ↔ 00109-9 의 계약 지점이며,
     * window.forwardCollectRecipients 로도 노출해 chip 입력 JS 가 재사용할 수
     * 있게 한다.
     *
     * @returns {string[]} 수신자 이메일 주소 배열 (chip 이 없으면 빈 배열).
     */
    function collectRecipients() {
        if (!chipsContainer) {
            return [];
        }
        var chips = chipsContainer.querySelectorAll('.forward-chip[data-email]');
        var emails = [];
        chips.forEach(function (chip) {
            var email = (chip.dataset.email || '').trim();
            if (email) {
                emails.push(email);
            }
        });
        return emails;
    }
    window.forwardCollectRecipients = collectRecipients;

    /**
     * 추가 메시지 textarea 의 현재 글자 수를 카운터에 반영한다.
     */
    function updateMessageCount() {
        if (messageCount) {
            messageCount.textContent = String(messageTextarea.value.length);
        }
    }

    /**
     * 인라인 에러 메시지와 결과 박스, 발송 진행 영역을 모두 초기화하고,
     * 자동 닫기 타이머 / polling 타이머를 정리한다 (task 00120).
     */
    function clearFeedback() {
        if (errorMsg) {
            errorMsg.textContent = '';
        }
        if (resultBox) {
            resultBox.textContent = '';
            resultBox.hidden = true;
            resultBox.classList.remove(
                'forward-modal__result--success',
                'forward-modal__result--error'
            );
        }
        if (autoCloseTimerId !== null) {
            window.clearTimeout(autoCloseTimerId);
            autoCloseTimerId = null;
        }
        // 발송 진행 영역과 polling 타이머도 함께 정리한다.
        stopProgressPolling();
        hideProgress();
    }

    /**
     * 발송 진행 영역을 숨기고 막대 / 텍스트 표시를 초기 상태로 되돌린다.
     * polling 타이머 정리는 ``stopProgressPolling`` 에서 별도로 처리한다.
     */
    function hideProgress() {
        if (progressBox) {
            progressBox.hidden = true;
        }
        if (progressBar) {
            progressBar.max = 1;
            progressBar.value = 0;
        }
        if (progressText) {
            progressText.textContent = '';
        }
    }

    /**
     * 진행 중인 polling 을 중단한다. 타이머 / 매칭용 식별자 / 연속 실패 카운터를
     * 모두 초기화한다. 진행 영역의 visibility 는 별도로 ``hideProgress`` 가
     * 담당하므로 본 함수는 건드리지 않는다 (예: 결과 박스 전환 직전에는 polling
     * 만 멈추고 진행 영역 표시는 다른 흐름에서 처리하기 위함).
     */
    function stopProgressPolling() {
        if (progressPollingTimerId !== null) {
            window.clearInterval(progressPollingTimerId);
            progressPollingTimerId = null;
        }
        progressPollingCanonicalId = null;
        progressPollingForwardLogId = null;
        progressPollingTotalRecipients = 0;
        progressPollingConsecutiveFailures = 0;
    }

    /**
     * GET /forward-logs/{id} 응답으로 받은 카운트로 막대와 카운터 텍스트를
     * 갱신한다. 실패 0건이면 ``· 실패 0건`` 표기는 생략한다.
     *
     * @param {number} successCount 누적 성공 수.
     * @param {number} failureCount 누적 실패 수.
     * @param {number} totalRecipients 전체 수신자 수.
     */
    function updateProgress(successCount, failureCount, totalRecipients) {
        if (!progressBox || !progressBar || !progressText) {
            return;
        }
        var safeTotal = totalRecipients > 0 ? totalRecipients : 1;
        var processed = successCount + failureCount;
        if (processed > safeTotal) {
            // 백엔드 / 클라이언트 사이 일시적 race 보호 — 막대가 100% 를 넘지
            // 않게 잘라낸다 (텍스트는 raw 값을 그대로 보여줘 디버깅 용이).
            progressBar.max = processed;
        } else {
            progressBar.max = safeTotal;
        }
        progressBar.value = processed;
        var label =
            processed +
            '/' +
            totalRecipients +
            ' 전송 완료';
        if (failureCount > 0) {
            label += ' · 실패 ' + failureCount + '건';
        }
        progressText.textContent = label;
        progressBox.hidden = false;
    }

    /**
     * 발송 결과 polling 을 시작한다. 즉시 1회 GET 으로 막대를 초기 상태에서
     * 갱신한 뒤, ``POLLING_INTERVAL_MS`` 간격으로 setInterval 을 건다.
     * 종료 조건:
     *   - 응답 status 가 'in_progress' 가 아니면(success / partial / failed)
     *     polling 을 중단하고 결과 박스로 전환한다.
     *   - HTTP 오류가 ``POLLING_MAX_CONSECUTIVE_FAILURES`` 회 연속이면 polling
     *     을 중단하고 네트워크 오류로 에러 박스를 띄운다. 단발 오류는 무시.
     *
     * @param {number|string} canonicalId 발송 대상 canonical PK.
     * @param {number} forwardLogId POST 응답의 forward_log_id.
     * @param {number} totalRecipients POST 요청 시점의 수신자 수.
     */
    function startProgressPolling(canonicalId, forwardLogId, totalRecipients) {
        // 이전 발송의 잔여 polling 이 있다면 정리한다 (방어).
        stopProgressPolling();
        progressPollingCanonicalId = canonicalId;
        progressPollingForwardLogId = forwardLogId;
        progressPollingTotalRecipients = totalRecipients;
        progressPollingConsecutiveFailures = 0;
        // 막대를 0/N 으로 즉시 표시한다 (첫 GET 응답 도착 전 placeholder).
        updateProgress(0, 0, totalRecipients);
        // 즉시 1회 + setInterval 반복.
        runProgressPollingTick();
        progressPollingTimerId = window.setInterval(
            runProgressPollingTick,
            POLLING_INTERVAL_MS
        );
    }

    /**
     * polling tick 1 회 — GET /api/canonical/{canonicalId}/forward-logs/{id}
     * 을 호출해 카운트를 갱신하고, 종료 조건을 평가한다. 응답 도착 시점에
     * polling 이 이미 중단된 상태(취소 / 모달 닫힘)면 무시한다.
     */
    function runProgressPollingTick() {
        var canonicalId = progressPollingCanonicalId;
        var forwardLogId = progressPollingForwardLogId;
        var totalRecipients = progressPollingTotalRecipients;
        // 모달이 닫혀 stopProgressPolling 이 먼저 호출됐을 수 있다 (이미 timer
        // 가 정리됐으면 이 함수가 다시 불릴 일은 없지만 방어).
        if (canonicalId === null || forwardLogId === null) {
            return;
        }
        fetch(
            '/api/canonical/' +
                encodeURIComponent(canonicalId) +
                '/forward-logs/' +
                encodeURIComponent(forwardLogId),
            { headers: { Accept: 'application/json' } }
        )
            .then(function (response) {
                return response
                    .json()
                    .catch(function () {
                        return {};
                    })
                    .then(function (data) {
                        return { ok: response.ok, status: response.status, data: data };
                    });
            })
            .then(function (result) {
                // 응답이 도착했을 때 polling 이 이미 종료됐다면(취소 / 모달
                // 닫힘) stale 응답이므로 화면을 건드리지 않는다.
                if (progressPollingForwardLogId !== forwardLogId) {
                    return;
                }
                if (!result.ok) {
                    handleProgressPollingHttpFailure(result.status);
                    return;
                }
                // 정상 응답 — 연속 실패 카운터 리셋.
                progressPollingConsecutiveFailures = 0;
                var data = result.data || {};
                var successCount =
                    typeof data.success_count === 'number' ? data.success_count : 0;
                var failureCount =
                    typeof data.failure_count === 'number' ? data.failure_count : 0;
                updateProgress(successCount, failureCount, totalRecipients);
                if (data.status === 'in_progress') {
                    return;
                }
                // 종료 — polling 중단 + 결과 박스 전환.
                stopProgressPolling();
                hideProgress();
                if (data.status === 'failed') {
                    // 전부 실패 — 에러 박스. handleSendFailure 는 detail 문자열을
                    // 우선하므로 카운트 메시지를 detail 로 직접 만들어 넘긴다.
                    handleSendFailure(0, {
                        detail:
                            '발송 실패 — 성공 ' +
                            successCount +
                            '명, 실패 ' +
                            failureCount +
                            '명',
                    });
                } else {
                    // success / partial — 성공 박스 + 카운트 표시 + 자동 닫기.
                    handleSendSuccess({
                        success_count: successCount,
                        failure_count: failureCount,
                    });
                }
            })
            .catch(function () {
                if (progressPollingForwardLogId !== forwardLogId) {
                    return;
                }
                // fetch 자체 실패 (네트워크 단절 등) — HTTP status 0 으로 흡수한다.
                handleProgressPollingHttpFailure(0);
            });
    }

    /**
     * polling 중 HTTP 오류 처리. 연속 실패가 임계치를 넘으면 polling 을 멈추고
     * 네트워크 오류 박스로 전환한다. 단발 오류는 카운터만 증가시키고 흘려보낸다.
     *
     * @param {number} httpStatus 응답 HTTP 상태 (0 = fetch 자체 실패).
     */
    function handleProgressPollingHttpFailure(httpStatus) {
        progressPollingConsecutiveFailures += 1;
        if (
            progressPollingConsecutiveFailures < POLLING_MAX_CONSECUTIVE_FAILURES
        ) {
            // 일시적 오류 — 다음 tick 에서 다시 시도한다.
            return;
        }
        stopProgressPolling();
        hideProgress();
        handleSendFailure(httpStatus, {
            detail: '네트워크 오류 — 발송 결과 확인 실패',
        });
    }

    /**
     * 발송 결과 박스를 표시한다.
     *
     * @param {string} kind 'success' | 'error'.
     * @param {string} message 사용자에게 보여줄 메시지.
     */
    function showResult(kind, message) {
        if (!resultBox) {
            return;
        }
        resultBox.textContent = message;
        resultBox.classList.remove(
            'forward-modal__result--success',
            'forward-modal__result--error'
        );
        resultBox.classList.add(
            kind === 'success'
                ? 'forward-modal__result--success'
                : 'forward-modal__result--error'
        );
        resultBox.hidden = false;
    }

    /**
     * 발송 진행 중 모달을 잠그거나 해제한다. 잠금 상태에서는 입력 / 버튼이
     * 비활성화되고 spinner 가 노출되며, 배경 클릭 / 취소로 닫히지 않는다.
     *
     * @param {boolean} locked true 면 잠금, false 면 해제.
     */
    function setModalLocked(locked) {
        isLocked = locked;
        modal.classList.toggle('forward-modal--locked', locked);
        sendBtn.disabled = locked;
        cancelBtn.disabled = locked;
        subjectInput.disabled = locked;
        messageTextarea.disabled = locked;
        if (organizationSelect) {
            organizationSelect.disabled = locked;
        }
        if (recipientInput) {
            recipientInput.disabled = locked;
        }
        if (locked) {
            // 발송 중에는 자동완성 드롭다운을 띄워 두지 않는다.
            hideAutocomplete();
        }
        if (previewToggle) {
            previewToggle.disabled = locked;
        }
        if (spinner) {
            spinner.hidden = !locked;
        }
        sendBtn.textContent = locked ? '발송 중…' : '발송';
    }

    /**
     * 본문 미리보기 영역을 펼친다 — iframe 내용을 즉시 갱신한다.
     */
    function expandPreview() {
        previewRegion.hidden = false;
        previewToggle.innerHTML = '&#9662; 본문 미리보기';
        renderPreview();
    }

    /**
     * 본문 미리보기 영역을 접는다.
     */
    function collapsePreview() {
        previewRegion.hidden = true;
        previewToggle.innerHTML = '&#9656; 본문 미리보기';
    }

    /**
     * 미리보기 메타 박스의 한 행(label / value)을 HTML 문자열로 만든다.
     *
     * @param {string} label 행 레이블 (예: '발주기관').
     * @param {string} value 행 값.
     * @returns {string} <tr> HTML 문자열.
     */
    function buildPreviewMetaRow(label, value) {
        return (
            '<tr><td style="color:#888;width:90px;padding:2px 0;">' +
            escapeHtml(label) +
            '</td><td>' +
            escapeHtml(value) +
            '</td></tr>'
        );
    }

    /**
     * 현재 모달 상태(제목 버튼 data-* 의 공고 메타 + 추가 메시지 textarea)로
     * 간이 HTML 본문을 조립해 미리보기 iframe 의 srcdoc 으로 주입한다.
     *
     * 서버의 build_forward_html_body 와 동일한 인라인 grayscale 디자인을
     * 흉내내지만, 공고 요약 / 상세 링크 등 서버만 아는 정보는 생략한다 — 그래서
     * "참고용 미리보기" 안내가 모달에 함께 표시된다.
     */
    function renderPreview() {
        var title = modal.dataset.announcementTitle || '';
        var agency = modal.dataset.announcementAgency || '';
        var statusText = modal.dataset.announcementStatus || '';
        var deadline = modal.dataset.announcementDeadline || '';
        var message = messageTextarea.value;

        var metaRows = '';
        if (agency) {
            metaRows += buildPreviewMetaRow('발주기관', agency);
        }
        if (statusText) {
            metaRows += buildPreviewMetaRow('상태', statusText);
        }
        if (deadline) {
            metaRows += buildPreviewMetaRow('마감일', deadline);
        }
        var metaBox = metaRows
            ? '<table style="width:100%;background:#f5f5f5;border-radius:6px;' +
              'padding:12px 16px;font-size:14px;border-collapse:collapse;">' +
              metaRows +
              '</table>'
            : '';

        var messageBox = '';
        if (message.trim()) {
            messageBox =
                '<div style="border-left:4px solid #888;padding:8px 14px;' +
                'margin:16px 0;background:#fafafa;font-size:14px;' +
                'white-space:pre-wrap;">' +
                '<div style="color:#888;font-size:12px;margin-bottom:4px;">' +
                '보낸 사람 메시지</div>' +
                escapeHtml(message) +
                '</div>';
        }

        var documentHtml =
            '<!doctype html><html lang="ko"><head><meta charset="utf-8"></head>' +
            '<body style="margin:0;">' +
            '<div style="max-width:600px;margin:0 auto;padding:24px;' +
            "font-family:system-ui,-apple-system,'Segoe UI',sans-serif;" +
            'color:#333;line-height:1.6;">' +
            '<h2 style="font-size:20px;margin:0 0 16px;">' +
            escapeHtml(title) +
            '</h2>' +
            metaBox +
            messageBox +
            '<div style="margin:24px 0;">' +
            '<span style="display:inline-block;background:#444;color:#fff;' +
            'padding:10px 20px;border-radius:6px;font-size:14px;">' +
            '공고 상세 보기</span></div>' +
            '<hr style="border:none;border-top:1px solid #e0e0e0;margin:24px 0;">' +
            '<div style="font-size:12px;color:#999;">' +
            '이 메일은 정부사업 모니터링 시스템에서 발송되었습니다.</div>' +
            '</div></body></html>';

        previewFrame.srcdoc = documentHtml;
    }

    // ──────────────────────────────────────────────────────────
    // 수신자 chip 입력 + 내부 사용자 자동완성 (00109-9)
    //
    // #forward-recipients-chips 안에 .forward-chip[data-email] 을 채우고 지우는
    // 책임을 진다. collectRecipients() 가 이 chip 들을 읽어 발송 수신자 목록을
    // 만든다 — 00109-8 과의 계약 지점.
    //   - 입력칸 타이핑 → 250ms debounce 후 GET /api/users/search 호출, 결과를
    //     드롭다운으로 표시. 항목 클릭 / Enter → chip 추가 (데이터는 이메일 주소).
    //   - 외부 이메일 직접 입력 + Enter / 콤마 → 간단 형식 검증 후 chip 추가.
    //     형식이 틀리면 입력 컨테이너에 빨간 border.
    //   - chip 우상단 X → 제거. 입력칸이 빈 상태에서 Backspace → 마지막 chip 제거.
    //   - chip 최대 50 개 (서버 제한과 일치). 초과 시 추가 차단 + 인라인 안내.
    // ──────────────────────────────────────────────────────────

    /**
     * 수신자 chip DOM 요소를 만든다. chip 의 식별 데이터는 항상 이메일 주소이며
     * (data-email), 화면에 보이는 라벨은 내부 사용자면 username, 외부 입력이면
     * 이메일 그대로다. 우상단 X 버튼으로 제거한다.
     *
     * @param {string} email chip 의 수신자 이메일 주소 (data-email 값).
     * @param {string} label 화면에 표시할 라벨 (없으면 이메일로 대체).
     * @returns {HTMLElement} .forward-chip 요소.
     */
    function createChipElement(email, label) {
        var chip = document.createElement('span');
        chip.className = 'forward-chip';
        chip.dataset.email = email;

        var labelSpan = document.createElement('span');
        labelSpan.className = 'forward-chip__label';
        labelSpan.textContent = label || email;
        // 라벨이 username 으로 줄어들어도 실제 발송 주소를 hover 로 확인할 수 있게 한다.
        labelSpan.title = email;

        var removeButton = document.createElement('button');
        removeButton.type = 'button';
        removeButton.className = 'forward-chip__remove';
        removeButton.setAttribute('aria-label', '수신자 제거');
        removeButton.textContent = '×';

        chip.appendChild(labelSpan);
        chip.appendChild(removeButton);
        return chip;
    }

    /**
     * 수신자 입력 컨테이너의 빨간 border(형식 오류 표시)를 켜거나 끈다.
     *
     * @param {boolean} invalid true 면 빨간 border, false 면 해제.
     */
    function setRecipientInputInvalid(invalid) {
        if (recipientsBox) {
            recipientsBox.classList.toggle('forward-recipients--invalid', !!invalid);
        }
    }

    /**
     * 수신자 chip 을 추가한다. 이미 같은 이메일(대소문자 무시)이 있으면 중복으로
     * 보고 추가하지 않으며, chip 이 이미 50 개면 한도 초과로 막는다.
     *
     * @param {string} email chip 으로 추가할 이메일 주소.
     * @param {string} label 화면에 표시할 라벨.
     * @returns {string} 'added' | 'duplicate' | 'limit'.
     */
    function addRecipientChip(email, label) {
        var normalizedEmail = (email || '').trim();
        if (!normalizedEmail) {
            return 'duplicate';
        }
        var lowerEmail = normalizedEmail.toLowerCase();
        var existingChips = chipsContainer.querySelectorAll('.forward-chip[data-email]');
        for (var index = 0; index < existingChips.length; index += 1) {
            var existingEmail = (existingChips[index].dataset.email || '')
                .trim()
                .toLowerCase();
            if (existingEmail === lowerEmail) {
                return 'duplicate';
            }
        }
        if (existingChips.length >= RECIPIENTS_MAX_COUNT) {
            if (errorMsg) {
                errorMsg.textContent =
                    '받는 사람은 최대 ' +
                    RECIPIENTS_MAX_COUNT +
                    '명까지 입력할 수 있습니다.';
            }
            return 'limit';
        }
        chipsContainer.appendChild(createChipElement(normalizedEmail, label));
        // chip 이 정상 추가되면 직전의 인라인 안내(한도 초과 등)는 더 이상 유효하지 않다.
        if (errorMsg && errorMsg.textContent) {
            errorMsg.textContent = '';
        }
        return 'added';
    }

    /**
     * #forward-recipients-chips 안의 모든 chip 을 제거한다. 모달을 새로 열 때
     * 이전 입력을 남기지 않기 위해 호출한다.
     */
    function clearRecipientChips() {
        if (chipsContainer) {
            chipsContainer.innerHTML = '';
        }
    }

    /**
     * 자동완성 드롭다운을 숨기고 내용을 비운다 + 키보드 강조 상태를 초기화한다.
     */
    function hideAutocomplete() {
        if (!autocompleteBox) {
            return;
        }
        autocompleteBox.hidden = true;
        autocompleteBox.innerHTML = '';
        autocompleteActiveIndex = -1;
    }

    /**
     * 자동완성 드롭다운에 검색 결과를 렌더한다. 결과가 없으면 "검색 결과가
     * 없습니다" 안내를 표시한다. 각 항목은 username + email + 조직명을 보여주며,
     * data-email 에 chip 으로 확정할 이메일 주소를 담는다. 모든 텍스트는
     * textContent 로 주입해 XSS 를 방지한다.
     *
     * @param {Array} users /api/users/search 응답 배열.
     */
    function renderAutocomplete(users) {
        if (!autocompleteBox) {
            return;
        }
        autocompleteBox.innerHTML = '';
        autocompleteActiveIndex = -1;

        if (!users.length) {
            var emptyRow = document.createElement('div');
            emptyRow.className = 'forward-autocomplete__empty';
            emptyRow.textContent = '검색 결과가 없습니다';
            autocompleteBox.appendChild(emptyRow);
            autocompleteBox.hidden = false;
            return;
        }

        users.forEach(function (user) {
            var item = document.createElement('button');
            item.type = 'button';
            item.className = 'forward-autocomplete__item';
            item.dataset.email = user && user.email ? user.email : '';

            var nameSpan = document.createElement('span');
            nameSpan.className = 'forward-autocomplete__name';
            nameSpan.textContent = user && user.username ? user.username : '';

            var emailSpan = document.createElement('span');
            emailSpan.className = 'forward-autocomplete__email';
            emailSpan.textContent = user && user.email ? user.email : '';

            item.appendChild(nameSpan);
            item.appendChild(emailSpan);

            var organizations =
                user && Array.isArray(user.organizations) ? user.organizations : [];
            if (organizations.length) {
                var organizationNames = organizations
                    .map(function (organization) {
                        return organization && organization.name
                            ? organization.name
                            : '';
                    })
                    .filter(function (name) {
                        return name;
                    });
                if (organizationNames.length) {
                    var orgSpan = document.createElement('span');
                    orgSpan.className = 'forward-autocomplete__orgs';
                    orgSpan.textContent = organizationNames.join(', ');
                    item.appendChild(orgSpan);
                }
            }
            autocompleteBox.appendChild(item);
        });
        autocompleteBox.hidden = false;
    }

    /**
     * 자동완성 드롭다운 항목을 chip 으로 확정한다. 항목의 username 을 라벨로,
     * data-email 을 chip 데이터로 쓴다. 추가 / 중복 / 한도 어느 결과든 입력칸과
     * 드롭다운은 정리한다.
     *
     * @param {HTMLElement} item .forward-autocomplete__item 요소.
     */
    function selectAutocompleteItem(item) {
        var email = (item.dataset.email || '').trim();
        if (!email) {
            return;
        }
        var nameSpan = item.querySelector('.forward-autocomplete__name');
        var label = nameSpan && nameSpan.textContent ? nameSpan.textContent : email;
        addRecipientChip(email, label);
        recipientInput.value = '';
        setRecipientInputInvalid(false);
        hideAutocomplete();
        recipientInput.focus();
    }

    /**
     * 키보드 ↑/↓ 로 자동완성 드롭다운의 강조 항목을 이동한다. 양 끝에서
     * 순환하며, 강조 항목이 보이도록 스크롤한다.
     *
     * @param {number} delta +1 이면 아래로, -1 이면 위로.
     * @param {NodeList} items 현재 드롭다운의 .forward-autocomplete__item 목록.
     */
    function moveAutocompleteActive(delta, items) {
        var count = items.length;
        if (!count) {
            return;
        }
        var nextIndex = autocompleteActiveIndex + delta;
        if (nextIndex < 0) {
            nextIndex = count - 1;
        } else if (nextIndex >= count) {
            nextIndex = 0;
        }
        items.forEach(function (item, index) {
            item.classList.toggle(
                'forward-autocomplete__item--active',
                index === nextIndex
            );
        });
        autocompleteActiveIndex = nextIndex;
        items[nextIndex].scrollIntoView({ block: 'nearest' });
    }

    /**
     * 입력칸에 직접 타이핑한 외부 이메일을 chip 으로 확정한다. 간단 형식 검증에
     * 실패하면 입력 컨테이너에 빨간 border 를 켜고 텍스트를 유지한다.
     */
    function commitTypedEmail() {
        var rawValue = recipientInput.value.trim();
        if (!rawValue) {
            return;
        }
        if (!EMAIL_PATTERN.test(rawValue)) {
            setRecipientInputInvalid(true);
            return;
        }
        var outcome = addRecipientChip(rawValue, rawValue);
        if (outcome === 'limit') {
            // 한도 초과 — 텍스트를 남겨 사용자가 상황을 인지하게 한다.
            return;
        }
        // 'added' / 'duplicate' 모두 입력칸은 비운다 (중복은 이미 chip 이 있음).
        recipientInput.value = '';
        setRecipientInputInvalid(false);
        hideAutocomplete();
    }

    /**
     * 내부 사용자 자동완성 검색을 1 회 수행한다. 응답이 도착했을 때 가장 최근
     * 요청인지(token), 그리고 입력칸이 여전히 포커스 상태인지 확인한 뒤에만
     * 드롭다운을 갱신해 stale 응답이 화면을 덮어쓰지 않게 한다.
     *
     * @param {string} query 검색어 (1~50 자).
     */
    function runUserSearch(query) {
        var requestToken = autocompleteRequestToken + 1;
        autocompleteRequestToken = requestToken;

        fetch(
            '/api/users/search?q=' +
                encodeURIComponent(query) +
                '&limit=' +
                USER_SEARCH_LIMIT,
            { headers: { Accept: 'application/json' } }
        )
            .then(function (response) {
                if (!response.ok) {
                    return [];
                }
                return response.json().catch(function () {
                    return [];
                });
            })
            .then(function (users) {
                if (requestToken !== autocompleteRequestToken) {
                    // 이후 더 최근 요청이 있었다 — stale 응답은 버린다.
                    return;
                }
                if (document.activeElement !== recipientInput) {
                    // 입력칸을 이미 떠났으면 드롭다운을 띄우지 않는다.
                    return;
                }
                renderAutocomplete(Array.isArray(users) ? users : []);
            })
            .catch(function () {
                if (requestToken === autocompleteRequestToken) {
                    hideAutocomplete();
                }
            });
    }

    /**
     * 수신자 입력칸 input 이벤트 핸들러. 빨간 border 를 풀고, 250ms debounce 후
     * 자동완성 검색을 건다. 검색어가 비었거나 서버 한도(50 자)를 넘으면
     * 드롭다운을 닫는다.
     */
    function handleRecipientInput() {
        setRecipientInputInvalid(false);
        if (autocompleteTimerId !== null) {
            window.clearTimeout(autocompleteTimerId);
            autocompleteTimerId = null;
        }
        var query = recipientInput.value.trim();
        if (query.length < 1 || query.length > USER_SEARCH_QUERY_MAX_LENGTH) {
            hideAutocomplete();
            return;
        }
        autocompleteTimerId = window.setTimeout(function () {
            autocompleteTimerId = null;
            runUserSearch(query);
        }, AUTOCOMPLETE_DEBOUNCE_MS);
    }

    /**
     * 수신자 입력칸 keydown 핸들러.
     *   - Enter: 드롭다운에서 강조 항목이 있으면 그 항목을 chip 으로, 없으면
     *     입력한 외부 이메일을 chip 으로 확정.
     *   - 콤마(,): 입력한 외부 이메일을 chip 으로 확정.
     *   - ↑/↓: 드롭다운 강조 항목 이동.
     *   - Escape: 드롭다운 닫기.
     *   - Backspace(입력칸이 빈 상태): 마지막 chip 제거.
     *
     * @param {KeyboardEvent} event keydown 이벤트.
     */
    function handleRecipientKeydown(event) {
        var key = event.key;

        if (key === 'Enter' || key === ',') {
            event.preventDefault();
            if (
                key === 'Enter' &&
                autocompleteBox &&
                !autocompleteBox.hidden &&
                autocompleteActiveIndex >= 0
            ) {
                var highlightedItems = autocompleteBox.querySelectorAll(
                    '.forward-autocomplete__item'
                );
                if (highlightedItems[autocompleteActiveIndex]) {
                    selectAutocompleteItem(highlightedItems[autocompleteActiveIndex]);
                    return;
                }
            }
            commitTypedEmail();
            return;
        }

        if (key === 'ArrowDown' || key === 'ArrowUp') {
            if (!autocompleteBox || autocompleteBox.hidden) {
                return;
            }
            var navigableItems = autocompleteBox.querySelectorAll(
                '.forward-autocomplete__item'
            );
            if (!navigableItems.length) {
                return;
            }
            event.preventDefault();
            moveAutocompleteActive(key === 'ArrowDown' ? 1 : -1, navigableItems);
            return;
        }

        if (key === 'Escape') {
            if (autocompleteBox && !autocompleteBox.hidden) {
                event.preventDefault();
                hideAutocomplete();
            }
            return;
        }

        if (key === 'Backspace' && recipientInput.value === '') {
            var chips = chipsContainer.querySelectorAll('.forward-chip');
            if (chips.length) {
                var lastChip = chips[chips.length - 1];
                lastChip.parentNode.removeChild(lastChip);
            }
        }
    }

    // 수신자 입력칸 — 타이핑(자동완성) / 키 입력(chip 확정·이동) / 포커스 이탈.
    if (recipientInput) {
        recipientInput.addEventListener('input', handleRecipientInput);
        recipientInput.addEventListener('keydown', handleRecipientKeydown);
        recipientInput.addEventListener('blur', function () {
            // 드롭다운 항목은 mousedown 에서 preventDefault 로 포커스를 유지하므로,
            // 진짜 포커스 이탈일 때만 약간의 지연 후 드롭다운을 닫는다.
            window.setTimeout(hideAutocomplete, 150);
        });
    }

    // 자동완성 드롭다운 — 항목 선택(클릭). mousedown 에서 처리해 입력칸 blur 보다
    // 먼저 동작하게 하고, preventDefault 로 포커스를 유지한다.
    if (autocompleteBox) {
        autocompleteBox.addEventListener('mousedown', function (event) {
            var item = event.target.closest('.forward-autocomplete__item');
            if (!item) {
                return;
            }
            event.preventDefault();
            selectAutocompleteItem(item);
        });
    }

    // chip 우상단 X 버튼 — 이벤트 위임으로 제거한다.
    if (chipsContainer) {
        chipsContainer.addEventListener('click', function (event) {
            var removeButton = event.target.closest('.forward-chip__remove');
            if (!removeButton) {
                return;
            }
            var chip = removeButton.closest('.forward-chip');
            if (chip) {
                chip.parentNode.removeChild(chip);
            }
            // chip 을 지웠으면 한도 초과 안내는 더 이상 유효하지 않다.
            if (errorMsg && errorMsg.textContent.indexOf('최대') !== -1) {
                errorMsg.textContent = '';
            }
            if (recipientInput) {
                recipientInput.focus();
            }
        });
    }

    // ──────────────────────────────────────────────────────────
    // 모달 열기
    // ──────────────────────────────────────────────────────────

    /**
     * "메일로 보내기" 버튼 클릭 시 모달을 연다.
     *
     * 버튼의 data-* (canonical-id / announcement-title / -agency / -status /
     * -deadline) 를 읽어 발송 대상 canonical 을 확정하고, 제목을 prefill 하며,
     * 미리보기용 공고 메타를 모달 dataset 에 보관한다. 추가 메시지 / 미리보기 /
     * 결과 박스는 매번 초기화한다 (입력 미유지).
     *
     * @param {HTMLElement} invokerEl 모달을 띄운 트리거 버튼.
     */
    window.openForwardModal = function (invokerEl) {
        currentCanonicalId = invokerEl.dataset.canonicalId || null;

        // 미리보기 조립에 쓸 공고 메타를 모달 dataset 에 옮겨 둔다.
        modal.dataset.announcementTitle = invokerEl.dataset.announcementTitle || '';
        modal.dataset.announcementAgency = invokerEl.dataset.announcementAgency || '';
        modal.dataset.announcementStatus = invokerEl.dataset.announcementStatus || '';
        modal.dataset.announcementDeadline =
            invokerEl.dataset.announcementDeadline || '';

        // 제목 prefill (사용자가 자유롭게 수정 가능).
        subjectInput.value = buildDefaultSubject(
            invokerEl.dataset.announcementTitle || ''
        );
        // 추가 메시지 초기화 + 카운터 갱신.
        messageTextarea.value = '';
        updateMessageCount();
        // 수신자 chip / 입력칸 / 자동완성 드롭다운 초기화 (입력 미유지).
        clearRecipientChips();
        if (recipientInput) {
            recipientInput.value = '';
        }
        setRecipientInputInvalid(false);
        hideAutocomplete();
        // 미리보기는 기본 접힘으로 초기화.
        collapsePreview();
        // 에러 / 결과 박스 초기화 + 자동 닫기 타이머 정리.
        clearFeedback();
        // 잠금 해제 + 발송 버튼 라벨 원복.
        setModalLocked(false);

        modal.showModal();
    };

    // 상세 페이지의 "메일로 보내기" 버튼에 클릭 핸들러를 건다.
    // 비로그인 시 버튼은 disabled 라 클릭 이벤트가 발생하지 않는다.
    document.querySelectorAll('.forward-open-btn').forEach(function (btn) {
        if (btn.disabled) {
            return;
        }
        btn.addEventListener('click', function (event) {
            event.preventDefault();
            window.openForwardModal(btn);
        });
    });

    // ──────────────────────────────────────────────────────────
    // 입력 인터랙션 — 글자 수 카운터 / 미리보기 toggle
    // ──────────────────────────────────────────────────────────

    messageTextarea.addEventListener('input', function () {
        updateMessageCount();
        // 미리보기가 펼쳐져 있으면 추가 메시지 변경을 즉시 반영한다.
        if (previewRegion && !previewRegion.hidden) {
            renderPreview();
        }
    });

    if (previewToggle) {
        previewToggle.addEventListener('click', function () {
            if (previewRegion.hidden) {
                expandPreview();
            } else {
                collapsePreview();
            }
        });
    }

    // ──────────────────────────────────────────────────────────
    // 발송 — 입력 검증 → POST → 결과 박스
    // ──────────────────────────────────────────────────────────

    /**
     * 발송 성공(HTTP 200) 응답을 처리한다.
     * 초록 결과 박스를 표시하고, 발송 이력 섹션 새로고침을 트리거한 뒤,
     * 3 초 후 모달을 자동으로 닫는다. 자동 닫기 전까지 모달은 잠긴 상태를
     * 유지한다 (입력 미유지).
     *
     * @param {Object} data 응답 본문 {forward_log_id, status, success_count,
     *     failure_count}.
     */
    function handleSendSuccess(data) {
        var successCount =
            data && typeof data.success_count === 'number'
                ? data.success_count
                : 0;
        var failureCount =
            data && typeof data.failure_count === 'number'
                ? data.failure_count
                : 0;
        showResult(
            'success',
            '발송 완료 — 성공 ' + successCount + '명, 실패 ' + failureCount + '명'
        );

        // 발송 이력 섹션 새로고침 (00109-10 이 window 에 정의한다).
        if (typeof window.refreshForwardHistory === 'function') {
            try {
                window.refreshForwardHistory();
            } catch (refreshError) {
                // 이력 새로고침 실패가 발송 결과 표시를 망가뜨리지 않게 한다.
            }
        }

        // 3 초 후 자동 닫기.
        autoCloseTimerId = window.setTimeout(function () {
            modal.close();
        }, AUTO_CLOSE_DELAY_MS);
    }

    /**
     * 발송 실패(HTTP 4xx/5xx) 응답을 처리한다. 모달 잠금을 풀고 빨간 결과
     * 박스를 표시한다 — 사용자가 입력을 고쳐 재시도할 수 있게 모달은 유지한다.
     *
     * @param {number} httpStatus 응답 HTTP 상태 코드.
     * @param {Object} data 응답 본문 (FastAPI 에러는 보통 {detail: ...}).
     */
    function handleSendFailure(httpStatus, data) {
        setModalLocked(false);
        var detail = data ? data.detail : null;
        // Pydantic 422 의 detail 은 배열일 수 있다 — 문자열일 때만 그대로 쓴다.
        if (typeof detail !== 'string') {
            detail = null;
        }
        var message =
            detail ||
            '발송 실패 (HTTP ' + httpStatus + ') — 입력을 확인하고 다시 시도해 주세요.';
        showResult('error', message);
    }

    form.addEventListener('submit', function (event) {
        // <form method="dialog"> 의 기본 닫기를 막고 fetch 흐름을 직접 제어한다.
        event.preventDefault();
        if (isLocked) {
            return;
        }
        if (!currentCanonicalId) {
            return;
        }

        clearFeedback();

        // ── 입력 검증 (서버 제한과 동일 기준) ──
        var recipients = collectRecipients();
        if (recipients.length < 1) {
            errorMsg.textContent = '받는 사람을 1명 이상 입력해 주세요.';
            return;
        }
        if (recipients.length > RECIPIENTS_MAX_COUNT) {
            errorMsg.textContent =
                '받는 사람은 최대 ' + RECIPIENTS_MAX_COUNT + '명까지 입력할 수 있습니다.';
            return;
        }
        var subject = subjectInput.value.trim();
        if (subject.length > SUBJECT_MAX_LENGTH) {
            errorMsg.textContent =
                '제목은 최대 ' + SUBJECT_MAX_LENGTH + '자까지 입력할 수 있습니다.';
            return;
        }
        var message = messageTextarea.value;
        if (message.length > MESSAGE_MAX_LENGTH) {
            errorMsg.textContent =
                '추가 메시지는 최대 ' +
                MESSAGE_MAX_LENGTH +
                '자까지 입력할 수 있습니다.';
            return;
        }

        var requestBody = {
            recipients: recipients,
            // 빈 제목이면 서버가 default 제목을 생성한다.
            subject: subject || null,
            // 공백뿐이면 추가 메시지 없음으로 보낸다.
            additional_message: message.trim() ? message : null,
            sender_organization_id: resolveOrganizationId(),
        };

        // 발송 중 모달 잠금 + spinner.
        setModalLocked(true);

        // ensure_same_origin 통과를 위해 same-origin 상대 경로로 fetch 한다.
        fetch('/api/canonical/' + encodeURIComponent(currentCanonicalId) + '/forward', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(requestBody),
        })
            .then(function (response) {
                return response
                    .json()
                    .catch(function () {
                        return {};
                    })
                    .then(function (data) {
                        return {
                            ok: response.ok,
                            status: response.status,
                            data: data,
                        };
                    });
            })
            .then(function (result) {
                if (!result.ok) {
                    handleSendFailure(result.status, result.data);
                    return;
                }
                var data = result.data || {};
                if (data.status === 'in_progress') {
                    // task 00120 — 발송 루프는 BackgroundTasks 로 비동기 실행
                    // 중. 모달 잠금을 유지한 채 polling 으로 진행 상황을 표시
                    // 한다. forward_log_id 가 빠진 비정상 응답은 방어적으로
                    // legacy 동기 흐름(handleSendSuccess)으로 떨어뜨린다.
                    if (typeof data.forward_log_id !== 'number') {
                        handleSendSuccess(data);
                        return;
                    }
                    startProgressPolling(
                        currentCanonicalId,
                        data.forward_log_id,
                        recipients.length
                    );
                    return;
                }
                // legacy / 즉시 완료 응답 — 동기 흐름과 동일하게 처리.
                handleSendSuccess(data);
            })
            .catch(function () {
                setModalLocked(false);
                showResult(
                    'error',
                    '네트워크 오류 — 발송하지 못했습니다. 다시 시도해 주세요.'
                );
            });
    });

    // ──────────────────────────────────────────────────────────
    // 닫기 — 취소 버튼 / 배경 클릭 / close 이벤트
    // ──────────────────────────────────────────────────────────

    cancelBtn.addEventListener('click', function () {
        // 발송 진행 중에는 닫지 않는다 (cancelBtn 은 이미 disabled 지만 방어).
        if (isLocked) {
            return;
        }
        modal.close();
    });

    // dialog 배경(backdrop) 클릭 → 닫기. 발송 진행 중에는 무시한다.
    modal.addEventListener('click', function (event) {
        if (event.target === modal && !isLocked) {
            modal.close();
        }
    });

    // 모달이 닫힐 때 — 자동 닫기 타이머 / polling 타이머 정리 + 발송 진행
    // 영역 숨김 + 잠금 상태 원복 (task 00120). 입력 내용은 다음
    // openForwardModal 호출이 다시 초기화하므로 여기서는 별도로 비우지 않는다.
    modal.addEventListener('close', function () {
        if (autoCloseTimerId !== null) {
            window.clearTimeout(autoCloseTimerId);
            autoCloseTimerId = null;
        }
        stopProgressPolling();
        hideProgress();
        setModalLocked(false);
    });
}());
