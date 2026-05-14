// 공고 상세 페이지 하단 "발송 이력" 섹션 (Phase A-2 Part 2 / task 00109-10).
//
// relevance.js / favorites.js / forward.js 와 동일하게 단일 IIFE 로 동작한다.
// base.html 이 모든 페이지에서 이 스크립트를 로드하지만, .forward-history-section
// 이 없는 페이지(목록, 관리자 페이지, canonical 없는 공고 등)에서는 초기화 직후
// early-return 한다.
//
// forward.js 와 별도 파일인 이유:
//   발송 이력 섹션은 비로그인 사용자에게도 노출되지만(GET endpoint 가 비로그인
//   허용), forward.js 는 #forward-modal(로그인 시에만 렌더)이 없으면 early-return
//   하므로 비로그인 페이지에서 함께 묶을 수 없다.
//
// 책임 범위 (00109-10 — 발송 이력 목록 + 행 expand):
//   - GET /api/canonical/{id}/forward-logs 로 발송 이력 목록을 불러와 테이블로
//     렌더한다. 이력이 없으면 빈 상태 안내 문구를 표시한다.
//   - 각 행 클릭(또는 ▾ 버튼) → 해당 행 아래 hidden 영역을 펼쳐
//     GET /api/canonical/{id}/forward-logs/{forward_log_id}/sends 로 수신자별
//     발송 결과를 불러와 표로 보여준다. 발송 결과는 immutable 이므로 첫 펼침에서
//     1 회만 호출하고 이후에는 캐시된 DOM 을 toggle 만 한다 (design note §9-m).
//
// 다른 subtask 와의 계약 지점:
//   - window.refreshForwardHistory: forward.js 의 발송 성공 핸들러가 호출한다.
//     발송이 끝나면 목록을 다시 불러와 방금 만든 row 가 즉시 보이게 한다.
(function () {
    'use strict';

    var section = document.querySelector('.forward-history-section');
    if (!section) {
        // 발송 이력 섹션이 없는 페이지 — 안전하게 종료한다.
        return;
    }

    var historyBody = document.getElementById('forward-history-body');
    var canonicalId = section.dataset.canonicalId || '';
    if (!historyBody || !canonicalId) {
        // canonical_id 가 없으면 포워딩 API 를 호출할 수 없다 — 무해하게 종료.
        return;
    }

    // ──────────────────────────────────────────────────────────
    // 헬퍼
    // ──────────────────────────────────────────────────────────

    /**
     * ISO-8601 UTC datetime 문자열을 KST 표시 문자열로 변환한다.
     *
     * `YYYY-MM-DD HH:MM:SS` 형식으로 출력 — admin_email.js 의 formatDateTimeKst
     * 및 다른 페이지의 KST 표시 컨벤션과 일관. Intl API 의 'en-CA' locale 이
     * 'YYYY-MM-DD' 와 24-hour 'HH:MM:SS' 를 모두 보장하며, Asia/Seoul timeZone
     * 으로 변환한다.
     *
     * @param {string|null|undefined} isoString ISO-8601 datetime 또는 falsy.
     * @returns {string} KST 표시 문자열 또는 빈 문자열 (falsy 입력).
     */
    function formatDateTimeKst(isoString) {
        if (!isoString) {
            return '';
        }
        var dateValue = new Date(isoString);
        if (isNaN(dateValue.getTime())) {
            // 파싱 실패 — raw 그대로 노출 (방어적).
            return String(isoString);
        }
        var formatted = dateValue.toLocaleString('en-CA', {
            timeZone: 'Asia/Seoul',
            year: 'numeric',
            month: '2-digit',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
            hour12: false,
        });
        // en-CA 는 'YYYY-MM-DD, HH:MM:SS' 형식 — 콤마 제거해 'YYYY-MM-DD HH:MM:SS'.
        return formatted.replace(', ', ' ').replace(',', ' ');
    }

    /**
     * 셀(td) 요소를 만들어 텍스트를 안전하게(textContent) 채운다.
     *
     * @param {string} text 셀에 넣을 텍스트.
     * @param {string} [className] 셀에 부여할 클래스명 (선택).
     * @returns {HTMLTableCellElement} 텍스트가 채워진 td 요소.
     */
    function createCell(text, className) {
        var cell = document.createElement('td');
        if (className) {
            cell.className = className;
        }
        cell.textContent = text === null || text === undefined ? '' : String(text);
        return cell;
    }

    /**
     * 발송 이력 1 건의 발송자 표시 문자열을 만든다.
     *
     * `발송자명 · 조직명` 형식이며, 조직이 NULL 이면 `발송자명 · (개인)`,
     * 발송자 User 가 탈퇴해 sender 가 NULL 이면 `(알 수 없음)` 으로 대체한다.
     *
     * @param {Object} forwardLog GET /forward-logs 응답 1 건.
     * @returns {string} 발송자 표시 문자열.
     */
    function buildSenderText(forwardLog) {
        var sender = forwardLog.sender;
        var senderName =
            sender && sender.username ? sender.username : '(알 수 없음)';
        var organization = forwardLog.sender_organization;
        var organizationName =
            organization && organization.name ? organization.name : '(개인)';
        return senderName + ' · ' + organizationName;
    }

    /**
     * 발송 이력 1 건의 상태 표시 문자열을 만든다.
     *
     * - success → '✅ 성공'
     * - partial → '⚠️ 부분 (성공수/수신자수)'
     * - failed  → '❌ 실패'
     * - 그 외   → 원본 status 문자열 그대로 (방어적).
     *
     * @param {Object} forwardLog GET /forward-logs 응답 1 건.
     * @returns {string} 상태 표시 문자열.
     */
    function buildStatusText(forwardLog) {
        var status = forwardLog.status;
        if (status === 'success') {
            return '✅ 성공';
        }
        if (status === 'partial') {
            var successCount =
                typeof forwardLog.success_count === 'number'
                    ? forwardLog.success_count
                    : 0;
            var recipientCount =
                typeof forwardLog.recipient_count === 'number'
                    ? forwardLog.recipient_count
                    : 0;
            return '⚠️ 부분 (' + successCount + '/' + recipientCount + ')';
        }
        if (status === 'failed') {
            return '❌ 실패';
        }
        return status ? String(status) : '-';
    }

    /**
     * 수신자별 발송 결과(EmailSendRun) 1 건의 상태 아이콘을 만든다.
     *
     * @param {string} status 'sent' | 'failed' | 그 외.
     * @returns {string} '✅ 성공' | '❌ 실패' | 원본 문자열.
     */
    function buildSendRunStatusText(status) {
        if (status === 'sent') {
            return '✅ 성공';
        }
        if (status === 'failed') {
            return '❌ 실패';
        }
        return status ? String(status) : '-';
    }

    // ──────────────────────────────────────────────────────────
    // 행 expand — 수신자별 발송 결과 표
    // ──────────────────────────────────────────────────────────

    /**
     * 수신자별 발송 결과(EmailSendRun 목록)를 표로 렌더한 요소를 만든다.
     * 모든 텍스트는 createCell(textContent) 로 주입해 XSS 를 방지한다.
     *
     * @param {Array} sendRuns GET /forward-logs/{id}/sends 응답 배열.
     * @returns {HTMLElement} 수신자별 발송 결과 표를 담은 요소.
     */
    function buildSendRunsTable(sendRuns) {
        if (!sendRuns.length) {
            var emptyBox = document.createElement('p');
            emptyBox.className = 'forward-history__sends-empty';
            emptyBox.textContent = '수신자별 발송 기록이 없습니다.';
            return emptyBox;
        }

        var table = document.createElement('table');
        table.className = 'forward-history__sends-table';

        var thead = document.createElement('thead');
        var headerRow = document.createElement('tr');
        var headerLabels = [
            { text: '받는 사람', className: 'forward-history__sends-col-recipient' },
            { text: '상태', className: 'forward-history__sends-col-status' },
            { text: '시도 횟수', className: 'forward-history__sends-col-attempt' },
            { text: '에러', className: 'forward-history__sends-col-error' },
            { text: '발송 시각', className: 'forward-history__sends-col-sent-at' },
        ];
        headerLabels.forEach(function (label) {
            var th = document.createElement('th');
            th.className = label.className;
            th.textContent = label.text;
            headerRow.appendChild(th);
        });
        thead.appendChild(headerRow);
        table.appendChild(thead);

        var tbody = document.createElement('tbody');
        sendRuns.forEach(function (sendRun) {
            var row = document.createElement('tr');
            row.appendChild(createCell(sendRun.recipient || '-'));
            row.appendChild(
                createCell(buildSendRunStatusText(sendRun.status))
            );
            row.appendChild(
                createCell(
                    typeof sendRun.attempt_count === 'number'
                        ? String(sendRun.attempt_count) + '회'
                        : '-'
                )
            );
            // 에러는 실패한 경우에만 채워지며, 없으면 '-' 로 표시한다.
            row.appendChild(
                createCell(
                    sendRun.error_message ? sendRun.error_message : '-',
                    'forward-history__sends-error'
                )
            );
            row.appendChild(createCell(formatDateTimeKst(sendRun.sent_at) || '-'));
            tbody.appendChild(row);
        });
        table.appendChild(tbody);

        return table;
    }

    /**
     * 발송 이력 1 행의 expand 영역을 펼치거나 접는다.
     *
     * 첫 펼침에서만 GET /forward-logs/{id}/sends 를 호출하고, 응답을 DOM 에
     * 보관한다(design note §9-m — 발송 결과는 immutable 이므로 재요청 불필요).
     * 두 번째 펼침부터는 캐시된 DOM 을 toggle 만 한다. 호출이 실패하면 캐시
     * 표시를 하지 않아 다음 펼침에서 재시도할 수 있게 한다.
     *
     * @param {HTMLTableRowElement} mainRow 발송 이력 main 행 (.forward-history__row).
     * @param {HTMLTableRowElement} expandRow main 행 바로 아래의 hidden 행.
     */
    function toggleExpand(mainRow, expandRow) {
        var expandCell = expandRow.querySelector('.forward-history__expand-cell');
        var toggleButton = mainRow.querySelector('.forward-history__toggle');

        if (!expandRow.hidden) {
            // 펼쳐진 상태 → 접는다.
            expandRow.hidden = true;
            mainRow.classList.remove('forward-history__row--expanded');
            if (toggleButton) {
                toggleButton.textContent = '▾';
                toggleButton.setAttribute('aria-label', '수신자별 결과 펼치기');
            }
            return;
        }

        // 접힌 상태 → 펼친다.
        expandRow.hidden = false;
        mainRow.classList.add('forward-history__row--expanded');
        if (toggleButton) {
            toggleButton.textContent = '▴';
            toggleButton.setAttribute('aria-label', '수신자별 결과 접기');
        }

        // 이미 한 번 불러왔으면(캐시) 추가 호출 없이 그대로 보여준다.
        if (expandRow.dataset.loaded === '1') {
            return;
        }

        var forwardLogId = mainRow.dataset.forwardLogId;
        expandCell.innerHTML = '';
        var loadingBox = document.createElement('p');
        loadingBox.className = 'forward-history__sends-loading';
        loadingBox.textContent = '수신자별 발송 결과를 불러오는 중…';
        expandCell.appendChild(loadingBox);

        fetch(
            '/api/canonical/' +
                encodeURIComponent(canonicalId) +
                '/forward-logs/' +
                encodeURIComponent(forwardLogId) +
                '/sends',
            { headers: { Accept: 'application/json' } }
        )
            .then(function (response) {
                if (!response.ok) {
                    throw new Error('HTTP ' + response.status);
                }
                return response.json();
            })
            .then(function (sendRuns) {
                expandCell.innerHTML = '';
                expandCell.appendChild(
                    buildSendRunsTable(Array.isArray(sendRuns) ? sendRuns : [])
                );
                // 발송 결과는 immutable — 한 번 성공적으로 불러오면 캐시한다.
                expandRow.dataset.loaded = '1';
            })
            .catch(function () {
                expandCell.innerHTML = '';
                var errorBox = document.createElement('p');
                errorBox.className = 'forward-history__sends-error-msg';
                errorBox.textContent =
                    '수신자별 발송 결과를 불러오지 못했습니다. 다시 펼쳐 주세요.';
                expandCell.appendChild(errorBox);
                // 캐시 표시를 하지 않아 다음 펼침에서 재시도된다.
            });
    }

    // ──────────────────────────────────────────────────────────
    // 발송 이력 목록 렌더
    // ──────────────────────────────────────────────────────────

    /**
     * 발송 이력 목록을 테이블로 렌더한다. 각 이력은 main 행 + 그 아래 hidden
     * expand 행 2 개로 구성되며, main 행을 클릭하거나 ▾ 버튼을 누르면 expand
     * 행이 펼쳐진다. 모든 동적 텍스트는 textContent 로 주입해 XSS 를 방지한다.
     *
     * @param {Array} forwardLogs GET /forward-logs 응답 배열.
     * @returns {HTMLElement} 발송 이력 테이블 요소.
     */
    function buildHistoryTable(forwardLogs) {
        var table = document.createElement('table');
        table.className = 'forward-history__table';

        var thead = document.createElement('thead');
        var headerRow = document.createElement('tr');
        var headerLabels = [
            { text: '시각', className: 'forward-history__col-time' },
            { text: '발송자', className: 'forward-history__col-sender' },
            { text: '수신자', className: 'forward-history__col-count' },
            { text: '상태', className: 'forward-history__col-status' },
            { text: '제목', className: 'forward-history__col-subject' },
            { text: '메시지', className: 'forward-history__col-message' },
            { text: '', className: 'forward-history__col-toggle' },
        ];
        headerLabels.forEach(function (label) {
            var th = document.createElement('th');
            th.className = label.className;
            th.textContent = label.text;
            headerRow.appendChild(th);
        });
        thead.appendChild(headerRow);
        table.appendChild(thead);

        var tbody = document.createElement('tbody');
        forwardLogs.forEach(function (forwardLog) {
            // ── main 행 ──
            var mainRow = document.createElement('tr');
            mainRow.className = 'forward-history__row';
            mainRow.dataset.forwardLogId = String(forwardLog.id);

            mainRow.appendChild(
                createCell(formatDateTimeKst(forwardLog.created_at) || '-')
            );
            mainRow.appendChild(createCell(buildSenderText(forwardLog)));
            mainRow.appendChild(
                createCell(
                    (typeof forwardLog.recipient_count === 'number'
                        ? forwardLog.recipient_count
                        : 0) + '명'
                )
            );
            mainRow.appendChild(createCell(buildStatusText(forwardLog)));

            // 제목 — truncate(CSS) + hover 시 전체 제목 tooltip.
            var subject = forwardLog.subject || '(제목 없음)';
            var subjectCell = createCell(subject, 'forward-history__subject');
            subjectCell.title = subject;
            mainRow.appendChild(subjectCell);

            // 메시지 — 추가 메시지가 있으면 📎, 없으면 '-'.
            mainRow.appendChild(
                createCell(
                    forwardLog.has_additional_message ? '📎' : '-',
                    'forward-history__message'
                )
            );

            // 펼침 ▾ 버튼.
            var toggleCell = document.createElement('td');
            toggleCell.className = 'forward-history__toggle-cell';
            var toggleButton = document.createElement('button');
            toggleButton.type = 'button';
            toggleButton.className = 'forward-history__toggle';
            toggleButton.setAttribute('aria-label', '수신자별 결과 펼치기');
            toggleButton.textContent = '▾';
            toggleCell.appendChild(toggleButton);
            mainRow.appendChild(toggleCell);

            // ── expand 행 (기본 hidden) ──
            var expandRow = document.createElement('tr');
            expandRow.className = 'forward-history__expand-row';
            expandRow.hidden = true;
            var expandCell = document.createElement('td');
            expandCell.className = 'forward-history__expand-cell';
            // main 행의 컬럼 수(7)와 맞춘다.
            expandCell.colSpan = 7;
            expandRow.appendChild(expandCell);

            // main 행 클릭(또는 ▾ 버튼) → expand toggle.
            mainRow.addEventListener('click', function () {
                toggleExpand(mainRow, expandRow);
            });

            tbody.appendChild(mainRow);
            tbody.appendChild(expandRow);
        });
        table.appendChild(tbody);

        return table;
    }

    /**
     * 발송 이력 섹션 본문을 주어진 상태(message)로 채운다 — 로딩 / 빈 상태 /
     * 에러 안내처럼 테이블이 아닌 단순 안내 문구를 표시할 때 쓴다.
     *
     * @param {string} className 안내 문구에 부여할 클래스명.
     * @param {string} message 표시할 안내 문구.
     */
    function showNotice(className, message) {
        historyBody.innerHTML = '';
        var notice = document.createElement('p');
        notice.className = className;
        notice.textContent = message;
        historyBody.appendChild(notice);
    }

    /**
     * GET /api/canonical/{id}/forward-logs 로 발송 이력 목록을 불러와 섹션을
     * 다시 렌더한다. 이력이 없으면 빈 상태 안내를, 호출이 실패하면 에러 안내를
     * 표시한다.
     */
    function loadForwardHistory() {
        showNotice(
            'forward-history__loading',
            '발송 이력을 불러오는 중…'
        );

        fetch(
            '/api/canonical/' +
                encodeURIComponent(canonicalId) +
                '/forward-logs',
            { headers: { Accept: 'application/json' } }
        )
            .then(function (response) {
                if (!response.ok) {
                    throw new Error('HTTP ' + response.status);
                }
                return response.json();
            })
            .then(function (forwardLogs) {
                var logs = Array.isArray(forwardLogs) ? forwardLogs : [];
                if (!logs.length) {
                    showNotice(
                        'forward-history__empty',
                        '이 공고는 아직 메일로 발송된 적이 없습니다.'
                    );
                    return;
                }
                historyBody.innerHTML = '';
                historyBody.appendChild(buildHistoryTable(logs));
            })
            .catch(function () {
                showNotice(
                    'forward-history__error',
                    '발송 이력을 불러오지 못했습니다. 페이지를 새로고침해 주세요.'
                );
            });
    }

    // forward.js 의 발송 성공 핸들러가 호출하는 새로고침 함수 — 발송이 끝나면
    // 목록을 다시 불러와 방금 만든 row 가 즉시 보이게 한다.
    window.refreshForwardHistory = loadForwardHistory;

    // 페이지 진입 시 1 회 자동 로드.
    loadForwardHistory();
}());
