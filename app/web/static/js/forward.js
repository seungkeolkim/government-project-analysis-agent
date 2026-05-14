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
    var errorMsg = document.getElementById('forward-error-msg');
    var resultBox = document.getElementById('forward-result-box');
    var sendBtn = document.getElementById('forward-send-btn');
    var spinner = document.getElementById('forward-spinner');
    var cancelBtn = document.getElementById('forward-cancel-btn');

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

    // 현재 모달이 대상으로 하는 canonical_project PK (문자열).
    var currentCanonicalId = null;
    // 발송 성공 후 자동 닫기 타이머 핸들 — 닫힐 때 정리한다.
    var autoCloseTimerId = null;
    // 발송 진행 중 모달 잠금 여부 — 잠겨 있으면 배경 클릭/취소로 닫지 않는다.
    var isLocked = false;

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
     * 인라인 에러 메시지와 결과 박스를 모두 초기화하고, 자동 닫기 타이머를
     * 정리한다.
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
                if (result.ok) {
                    handleSendSuccess(result.data);
                } else {
                    handleSendFailure(result.status, result.data);
                }
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

    // 모달이 닫힐 때 — 자동 닫기 타이머 정리 + 잠금 상태 원복.
    // 입력 내용은 다음 openForwardModal 호출이 다시 초기화하므로 여기서는
    // 별도로 비우지 않는다.
    modal.addEventListener('close', function () {
        if (autoCloseTimerId !== null) {
            window.clearTimeout(autoCloseTimerId);
            autoCloseTimerId = null;
        }
        setModalLocked(false);
    });
}());
