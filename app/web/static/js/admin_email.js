// 관리자 「메일 발송」 탭 인터랙션 (Phase A-1 / task 00104-11~13).
//
// 본 파일은 3 개 섹션의 init 함수를 들고 있다 — 각 섹션은 별도 subtask 에서
// 단계적으로 도입된다.
//   - initSettingsSection — 메일 설정 form (task 00104-11, 본 subtask 산출물)
//   - initTestSendSection — 테스트 발송 (task 00104-12, 미구현)
//   - initSendRunsSection — 발송 이력 (task 00104-13, 미구현)
//
// 외부 의존성 없음 — vanilla JS ES5+ 호환. fetch / async-await 대신 then 체인
// 으로 통일해 기존 progress.js / relevance.js 와 같은 톤 유지.
//
// 모든 fetch 호출은 `credentials: 'same-origin'` 을 명시해 세션 쿠키 전달.
// PUT/POST 는 admin_user_required + ensure_same_origin 두 dependency 가 묶여
// 있어 비관리자 또는 cross-origin 요청은 403/400 으로 차단된다.
(function () {
    'use strict';

    // ──────────────────────────────────────────────────────────
    // 공통 상수 / 헬퍼
    // ──────────────────────────────────────────────────────────

    var SETTINGS_URL = '/api/admin/email/settings';

    /**
     * 응답 본문에서 사용자에게 보여 줄 에러 메시지를 추출한다.
     *
     * 응답 형식:
     *   - {detail: \"...문자열...\"}             → 그대로 반환
     *   - {detail: [{loc:[...], msg:\"...\"}]}   → \"loc1.loc2: msg\" 형식으로 합침
     *   - 그 외                                  → \"HTTP <status> <statusText>\"
     */
    function extractErrorMessage(response, body) {
        if (body && typeof body === 'object') {
            var detail = body.detail;
            if (typeof detail === 'string') {
                return detail;
            }
            if (Array.isArray(detail)) {
                return detail.map(function (item) {
                    var location = '';
                    if (item && Array.isArray(item.loc)) {
                        // FastAPI 의 Pydantic 검증 에러는 loc[0] 이 'body' 또는 'query'.
                        // 사용자에게는 그 뒤만 보여주는 게 깔끔.
                        var locationParts = item.loc.slice(1);
                        if (locationParts.length > 0) {
                            location = locationParts.join('.') + ': ';
                        }
                    }
                    return location + (item && item.msg ? item.msg : '');
                }).join('; ');
            }
        }
        return 'HTTP ' + response.status + ' ' + response.statusText;
    }

    /**
     * flash 영역에 success / error 박스를 그린다. 기존 박스는 제거되고 새 박스로 교체.
     *
     * @param {'success'|'error'} kind 박스 종류.
     * @param {string} message 사용자에게 보여 줄 한글 메시지.
     */
    function showFlash(kind, message) {
        var area = document.getElementById('email-flash-area');
        if (!area) {
            return;
        }
        area.innerHTML = '';
        var box = document.createElement('div');
        box.className = 'admin-flash admin-flash--' + kind;
        box.setAttribute('role', kind === 'error' ? 'alert' : 'status');
        box.textContent = message;
        area.appendChild(box);
    }

    /**
     * flash 영역을 비운다 (새 요청 시작 시 호출).
     */
    function clearFlash() {
        var area = document.getElementById('email-flash-area');
        if (area) {
            area.innerHTML = '';
        }
    }

    /**
     * fetch 응답에서 JSON 본문을 안전하게 파싱해 { resp, body } 형태로 반환.
     * JSON 이 아닌 응답(예: 5xx HTML) 은 body=null 로 fallback.
     */
    function parseJsonResponse(response) {
        return response.text().then(function (text) {
            var body = null;
            if (text) {
                try {
                    body = JSON.parse(text);
                } catch (parseError) {
                    body = null;
                }
            }
            return { resp: response, body: body };
        });
    }

    // ──────────────────────────────────────────────────────────
    // 섹션 1: 메일 설정 (task 00104-11)
    // ──────────────────────────────────────────────────────────

    /**
     * 「메일 설정」 섹션을 초기화한다.
     *
     * - 페이지 로드 시 GET /api/admin/email/settings 로 현재 값을 받아 form 채움.
     * - client_secret 은 응답의 ``client_secret_masked`` 값을 placeholder 에 표시,
     *   입력 자체는 disabled 상태 (placeholder \"기존 값 유지 (****1234)\" 형식).
     * - [변경] 버튼 토글 ON → 입력 활성화 + placeholder \"새 값을 입력하세요\".
     *   토글 OFF (저장 후 자동 포함) → 입력 disabled + placeholder 복원.
     * - [저장] 클릭 → PUT /api/admin/email/settings.
     *   client_secret 토글이 OFF 이거나 빈 입력이면 body 에서 omit (서버가 기존 값 유지).
     *   성공 시 응답값으로 form 갱신 + 토글 OFF + success flash.
     *   실패 시 error flash + 응답 detail.
     *
     * 페이지에 form 요소가 없으면 (다른 탭) 즉시 반환 — 멱등 안전.
     */
    function initSettingsSection() {
        var form = document.getElementById('email-settings-form');
        if (!form) {
            return;
        }

        var tenantIdInput = document.getElementById('email-tenant-id');
        var clientIdInput = document.getElementById('email-client-id');
        var clientSecretInput = document.getElementById('email-client-secret');
        var clientSecretToggle = document.getElementById('email-client-secret-toggle');
        var senderAddressInput = document.getElementById('email-sender-address');
        var fromDisplayNameInput = document.getElementById('email-from-display-name');
        var maxRetryCountInput = document.getElementById('email-max-retry-count');
        var saveButton = document.getElementById('email-settings-save');

        // ── 초기 로드 ─────────────────────────────────────────
        loadSettings();

        // ── [변경] 토글 ──────────────────────────────────────
        clientSecretToggle.addEventListener('click', function () {
            var currentlyPressed =
                clientSecretToggle.getAttribute('aria-pressed') === 'true';
            setSecretEditMode(!currentlyPressed);
        });

        // ── 폼 submit ────────────────────────────────────────
        form.addEventListener('submit', function (event) {
            event.preventDefault();
            saveSettings();
        });

        /**
         * client_secret 입력의 편집 모드 ON/OFF 를 전환한다.
         *
         * @param {boolean} editMode true 면 입력 활성화 + 빈 값 + \"새 값을 입력하세요\" placeholder.
         *                          false 면 disabled + 빈 값 + masked placeholder 복원.
         */
        function setSecretEditMode(editMode) {
            if (editMode) {
                clientSecretInput.disabled = false;
                clientSecretInput.value = '';
                clientSecretInput.placeholder = '새 값을 입력하세요';
                clientSecretToggle.setAttribute('aria-pressed', 'true');
                clientSecretToggle.textContent = '취소';
                clientSecretInput.focus();
            } else {
                clientSecretInput.disabled = true;
                clientSecretInput.value = '';
                // applySettingsToForm 이 마지막에 설정한 masked placeholder 로 복원.
                // 이 값이 없으면 (초기 로드 실패 등) 안전한 default 사용.
                var maskedPlaceholder =
                    clientSecretInput.dataset.maskedPlaceholder || '기존 값 유지';
                clientSecretInput.placeholder = maskedPlaceholder;
                clientSecretToggle.setAttribute('aria-pressed', 'false');
                clientSecretToggle.textContent = '변경';
            }
        }

        /**
         * 응답 settings dict 를 form 입력에 반영한다.
         *
         * @param {object} settings GET / PUT 응답 형식의 EmailSettingsOut.
         */
        function applySettingsToForm(settings) {
            var m365 = (settings && settings.m365) || {};
            tenantIdInput.value = m365.tenant_id || '';
            clientIdInput.value = m365.client_id || '';

            // client_secret 은 mask 문자열을 placeholder 에 표시 (입력은 disabled).
            var mask = m365.client_secret_masked || '';
            var maskedPlaceholder = mask
                ? '기존 값 유지 (' + mask + ')'
                : '기존 값 유지 (미설정)';
            clientSecretInput.placeholder = maskedPlaceholder;
            clientSecretInput.dataset.maskedPlaceholder = maskedPlaceholder;
            clientSecretInput.value = '';
            // 저장 직후라면 toggle 도 자동 OFF 로 복원.
            setSecretEditMode(false);

            senderAddressInput.value = m365.sender_address || '';
            fromDisplayNameInput.value = settings.from_display_name || '';
            // max_retry_count 가 null/undefined 인 경우 default 2.
            var maxRetryValue = settings.max_retry_count;
            maxRetryCountInput.value =
                maxRetryValue != null ? String(maxRetryValue) : '2';
        }

        /**
         * GET /api/admin/email/settings — 페이지 로드 시 또는 저장 직후 호출.
         * 응답값으로 form 을 채운다. 실패 시 error flash.
         */
        function loadSettings() {
            clearFlash();
            saveButton.disabled = true;
            fetch(SETTINGS_URL, {
                method: 'GET',
                credentials: 'same-origin',
                headers: { 'Accept': 'application/json' }
            })
                .then(parseJsonResponse)
                .then(function (result) {
                    if (!result.resp.ok) {
                        throw new Error(
                            extractErrorMessage(result.resp, result.body)
                        );
                    }
                    applySettingsToForm(result.body || {});
                })
                .catch(function (error) {
                    showFlash(
                        'error',
                        '메일 설정 조회 실패: ' + (error.message || error)
                    );
                })
                .then(function () {
                    saveButton.disabled = false;
                });
        }

        /**
         * PUT /api/admin/email/settings — [저장] 버튼 클릭 시 호출.
         *
         * client_secret 정책:
         *   - 토글이 ON 이고 입력값이 비어 있지 않으면 body 에 포함 (값 변경).
         *   - 토글이 OFF 이거나 입력값이 비어 있으면 body 에서 omit (서버 기존값 유지).
         *
         * max_retry_count 는 number input 에서 정수로 캐스트해 보낸다. 빈 입력은
         * parseInt 결과 NaN 이 되며 Pydantic 422 로 거절되므로, JS 측에서 사전
         * 검증 후 사용자 친화 메시지로 막아도 좋다 (간단성 우선해 서버 측 검증에 위임).
         */
        function saveSettings() {
            clearFlash();
            saveButton.disabled = true;

            var requestBody = {
                m365: {
                    tenant_id: tenantIdInput.value.trim(),
                    client_id: clientIdInput.value.trim(),
                    sender_address: senderAddressInput.value.trim()
                },
                from_display_name: fromDisplayNameInput.value.trim(),
                max_retry_count: parseInt(maxRetryCountInput.value, 10)
            };

            // client_secret 토글 ON + 비어있지 않을 때만 body 에 포함.
            // (omit 하면 서버가 기존 값 유지 — 디자인 노트 §4-3 결정.)
            var inEditMode =
                clientSecretToggle.getAttribute('aria-pressed') === 'true';
            if (inEditMode && clientSecretInput.value !== '') {
                requestBody.m365.client_secret = clientSecretInput.value;
            }

            fetch(SETTINGS_URL, {
                method: 'PUT',
                credentials: 'same-origin',
                headers: {
                    'Accept': 'application/json',
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(requestBody)
            })
                .then(parseJsonResponse)
                .then(function (result) {
                    if (!result.resp.ok) {
                        throw new Error(
                            extractErrorMessage(result.resp, result.body)
                        );
                    }
                    // 성공 — 받은 새 값으로 form 갱신 (mask 재표시 + 토글 OFF).
                    applySettingsToForm(result.body || {});
                    showFlash('success', '메일 설정이 저장되었습니다.');
                })
                .catch(function (error) {
                    showFlash(
                        'error',
                        '저장 실패: ' + (error.message || error)
                    );
                })
                .then(function () {
                    saveButton.disabled = false;
                });
        }
    }

    // ──────────────────────────────────────────────────────────
    // 섹션 2: 테스트 발송 (task 00104-12)
    // ──────────────────────────────────────────────────────────

    var TEST_SEND_URL = '/api/admin/email/test-send';

    /**
     * 「테스트 발송」 섹션을 초기화한다.
     *
     * - submit 시 POST /api/admin/email/test-send.
     *   body = { recipient, subject, body } (trim 후 전송, body 는 newline 보존).
     * - 발송 중: 버튼 disabled + spinner + 텍스트 '발송 중...'.
     * - 응답 처리:
     *   - 200 + {success: true, send_run_id, message}: 초록색 박스
     *     '발송 성공 (send_run_id: N). 수신함과 정크메일 폴더를 모두 확인해주세요.'
     *     spec 의 한글 문구 그대로.
     *   - 4xx/5xx: 빨간색 박스 두 줄
     *     1) '발송 실패 (HTTP <status>): <detail>'
     *     2) '아래 「발송 이력」 섹션에서 상세 정보를 확인할 수 있습니다.'
     *   - fetch 자체 실패 (네트워크): 빨간색 박스, 동일 포맷.
     *
     * 페이지에 form 요소가 없으면 즉시 반환 (다른 탭에서 admin_email.js 가 잘못
     * 로드된 경우 안전).
     */
    function initTestSendSection() {
        var form = document.getElementById('email-test-send-form');
        if (!form) {
            return;
        }

        var recipientInput = document.getElementById('test-send-recipient');
        var subjectInput = document.getElementById('test-send-subject');
        var bodyInput = document.getElementById('test-send-body');
        var sendButton = document.getElementById('test-send-button');
        var resultArea = document.getElementById('test-send-result-area');
        // 발송 중 버튼 텍스트를 복구하기 위해 default 마크업을 미리 저장.
        var defaultButtonHtml = sendButton.innerHTML;

        form.addEventListener('submit', function (event) {
            event.preventDefault();
            performTestSend();
        });

        /**
         * 발송 중 / 대기 상태에 따라 버튼 모양과 disabled 를 전환한다.
         *
         * @param {boolean} isSending true 면 spinner + '발송 중...' + disabled.
         *                            false 면 default 마크업 복구 + enabled.
         */
        function setSendingState(isSending) {
            if (isSending) {
                sendButton.disabled = true;
                // spinner 는 .admin-button__spinner CSS 가 회전 애니메이션을 그린다.
                // span 안에 텍스트가 없도록 비워두고, aria-hidden 으로 SR 에서 무시.
                sendButton.innerHTML =
                    '<span class="admin-button__spinner" aria-hidden="true"></span>발송 중...';
            } else {
                sendButton.disabled = false;
                sendButton.innerHTML = defaultButtonHtml;
            }
        }

        /**
         * 결과 박스 영역을 비운다 (새 발송 시작 시 직전 결과 제거).
         */
        function clearResult() {
            resultArea.innerHTML = '';
        }

        /**
         * 결과 박스를 그린다. 여러 줄은 <br> 로 분리해 한 박스에 묶는다.
         *
         * @param {'success'|'error'} kind admin-flash--success / admin-flash--error.
         * @param {string[]} lines 사용자에게 보여 줄 한글 메시지 줄들.
         */
        function showResult(kind, lines) {
            resultArea.innerHTML = '';
            var box = document.createElement('div');
            box.className = 'admin-flash admin-flash--' + kind;
            box.setAttribute(
                'role', kind === 'error' ? 'alert' : 'status'
            );
            for (var index = 0; index < lines.length; index += 1) {
                if (index > 0) {
                    box.appendChild(document.createElement('br'));
                }
                box.appendChild(document.createTextNode(lines[index]));
            }
            resultArea.appendChild(box);
        }

        /**
         * POST /api/admin/email/test-send 본 호출. 성공/실패에 따라 결과 박스 갱신.
         *
         * body 의 newline 은 보존 (서버가 plain text 본문에 그대로 사용).
         * subject 는 trim 없이 그대로 (사용자가 의도적으로 trailing space 를 두는
         * 경우는 거의 없으나, 길이 검증은 서버 Pydantic 이 maxlength 로 검사).
         */
        function performTestSend() {
            clearResult();
            setSendingState(true);

            var requestBody = {
                recipient: recipientInput.value.trim(),
                subject: subjectInput.value,
                body: bodyInput.value
            };

            fetch(TEST_SEND_URL, {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'Accept': 'application/json',
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(requestBody)
            })
                .then(parseJsonResponse)
                .then(function (result) {
                    var resp = result.resp;
                    var responseBody = result.body || {};
                    if (resp.ok) {
                        // 성공 — spec 그대로의 한글 안내.
                        var sendRunId = responseBody.send_run_id;
                        showResult('success', [
                            '발송 성공 (send_run_id: ' + sendRunId +
                                '). 수신함과 정크메일 폴더를 모두 확인해주세요.'
                        ]);
                    } else {
                        // 실패 — HTTP status + detail + 발송 이력 안내 두 줄.
                        var detail = extractErrorMessage(resp, responseBody);
                        showResult('error', [
                            '발송 실패 (HTTP ' + resp.status + '): ' + detail,
                            '아래 「발송 이력」 섹션에서 상세 정보를 확인할 수 있습니다.'
                        ]);
                    }
                })
                .catch(function (error) {
                    // fetch 자체 실패 (네트워크 단절 등) — 응답 객체가 없으므로
                    // HTTP status 를 명시하지 않고 일반 메시지로.
                    showResult('error', [
                        '발송 요청 실패: ' + (error.message || error),
                        '네트워크 연결을 확인하거나 잠시 후 다시 시도해주세요.'
                    ]);
                })
                .then(function () {
                    // finally 대신 then 한 번 더 — 성공/실패/예외 모두 마무리 단계 보장.
                    setSendingState(false);
                    // 「발송 이력」 섹션에 새로고침 신호. 성공/실패 모두 row 가
                    // commit 되어 있으므로 동일하게 통지한다. 섹션 3 (00104-13) 의
                    // initSendRunsSection 이 이 이벤트를 청취해 자동 재조회한다.
                    dispatchTestSendCompletedEvent();
                });
        }

        /**
         * 발송 완료 (성공/실패 무관) 직후 window 에 custom event 를 dispatch 한다.
         * 「발송 이력」 섹션이 이 이벤트를 청취해 테이블을 자동 재로드한다.
         */
        function dispatchTestSendCompletedEvent() {
            // CustomEvent 가 지원되지 않는 환경은 admin 페이지의 다른 기능도
            // 동작하지 않으므로 안전한 fallback 으로 try/catch 만 둔다.
            try {
                var event = new CustomEvent('email-test-send-completed');
                window.dispatchEvent(event);
            } catch (dispatchError) {
                // 옛 브라우저에서 CustomEvent 실패 시 silent — 새로고침 버튼으로
                // 사용자가 수동 갱신할 수 있다.
            }
        }
    }

    // ──────────────────────────────────────────────────────────
    // 섹션 3: 발송 이력 (task 00104-13)
    // ──────────────────────────────────────────────────────────

    var SEND_RUNS_URL = '/api/admin/email/send-runs';
    var SEND_RUNS_LIMIT = 50;

    /**
     * ISO-8601 UTC datetime 문자열을 KST 표시 문자열로 변환한다.
     *
     * `YYYY-MM-DD HH:MM:SS` 형식으로 출력 — 다른 admin 페이지(scrape 등) 의
     * KST 표시 컨벤션과 일관. Intl API 의 'en-CA' locale 이 'YYYY-MM-DD' 와
     * 24-hour 'HH:MM:SS' 를 모두 보장하며, Asia/Seoul timeZone 으로 변환한다.
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
        // en-CA 는 'YYYY-MM-DD, HH:MM:SS' 형식 반환 — 콤마 제거해 'YYYY-MM-DD HH:MM:SS'.
        var formatted = dateValue.toLocaleString('en-CA', {
            timeZone: 'Asia/Seoul',
            year: 'numeric',
            month: '2-digit',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
            hour12: false
        });
        return formatted.replace(', ', ' ').replace(',', ' ');
    }

    /**
     * error_message 의 첫 줄만 추출한다 (truncate 표시용).
     *
     * 여러 줄 예외 메시지 (특히 traceback) 가 들어 있어도 첫 줄만 노출하며,
     * 전체는 td 의 title 속성에 들어가 tooltip 으로 확인 가능.
     */
    function getErrorMessageFirstLine(errorMessage) {
        if (!errorMessage) {
            return '';
        }
        var newlineIndex = errorMessage.indexOf('\n');
        return newlineIndex === -1
            ? errorMessage
            : errorMessage.substring(0, newlineIndex);
    }

    /**
     * 상태에 따라 ✅성공 / ❌실패 배지 span 을 만든다. 다른 status 값은 회색.
     *
     * .admin-badge--running (초록) 을 success 에, .admin-badge--idle (회색) 을
     * failure 에 사용 — backup 페이지가 같은 방식으로 hi 성공/실패 색을 매핑한다.
     */
    function buildStatusBadge(status) {
        var span = document.createElement('span');
        if (status === 'sent') {
            span.className = 'admin-badge admin-badge--running';
            span.textContent = '✅ 성공';
        } else if (status === 'failed') {
            span.className = 'admin-badge admin-badge--idle';
            span.textContent = '❌ 실패';
        } else {
            // 미래 status 값 (예: pending) 방어 — 회색 + raw 값.
            span.className = 'admin-badge';
            span.textContent = status ? String(status) : '?';
        }
        return span;
    }

    /**
     * EmailSendRun 1 row 를 <tr> 로 변환한다. 모든 user-provided 필드는
     * textContent / title 로 set 해 XSS 를 차단한다.
     */
    function buildSendRunsTableRow(item) {
        var tr = document.createElement('tr');

        // 시각 (KST)
        var timeTd = document.createElement('td');
        timeTd.textContent = formatDateTimeKst(item.created_at);
        tr.appendChild(timeTd);

        // 받는 사람
        var recipientTd = document.createElement('td');
        recipientTd.textContent = item.recipient || '';
        tr.appendChild(recipientTd);

        // 제목 — truncate + tooltip
        var subjectTd = document.createElement('td');
        var subjectFull = item.subject || '';
        subjectTd.title = subjectFull;
        var subjectSpan = document.createElement('span');
        subjectSpan.className = 'admin-email-runs-truncate';
        subjectSpan.textContent = subjectFull;
        subjectTd.appendChild(subjectSpan);
        tr.appendChild(subjectTd);

        // 상태 — ✅/❌ 배지
        var statusTd = document.createElement('td');
        statusTd.appendChild(buildStatusBadge(item.status));
        tr.appendChild(statusTd);

        // 시도 횟수
        var attemptCountTd = document.createElement('td');
        attemptCountTd.textContent =
            item.attempt_count != null ? String(item.attempt_count) : '';
        tr.appendChild(attemptCountTd);

        // 에러 — 실패 시 첫 줄 + truncate + tooltip 에 full text.
        var errorTd = document.createElement('td');
        if (item.error_message) {
            errorTd.title = item.error_message;
            var errorSpan = document.createElement('span');
            errorSpan.className = 'admin-email-runs-truncate';
            errorSpan.textContent = getErrorMessageFirstLine(item.error_message);
            errorTd.appendChild(errorSpan);
        }
        tr.appendChild(errorTd);

        // 발송자 — username, 시스템 자동 (requested_by_user_id NULL) 이면 '(자동)'.
        var requestedByTd = document.createElement('td');
        requestedByTd.textContent =
            item.requested_by_username || '(자동)';
        tr.appendChild(requestedByTd);

        return tr;
    }

    /**
     * items 배열을 받아 테이블 영역을 동적으로 렌더한다.
     * - items.length === 0: 빈 상태 텍스트 ('발송 이력이 없습니다.').
     * - 그 외: admin-table 풀 테이블.
     *
     * 호출 전에 호출자가 errors / loading 상태를 정리해야 한다.
     */
    function renderSendRunsTable(tableArea, items) {
        tableArea.innerHTML = '';

        if (!items || items.length === 0) {
            var emptyMessage = document.createElement('p');
            emptyMessage.className = 'admin-state__muted';
            emptyMessage.textContent = '발송 이력이 없습니다.';
            tableArea.appendChild(emptyMessage);
            return;
        }

        var table = document.createElement('table');
        table.className = 'admin-table';

        var thead = document.createElement('thead');
        var headerRow = document.createElement('tr');
        var headers = [
            '시각 (KST)', '받는 사람', '제목', '상태',
            '시도 횟수', '에러', '발송자'
        ];
        for (var headerIndex = 0; headerIndex < headers.length; headerIndex += 1) {
            var th = document.createElement('th');
            th.textContent = headers[headerIndex];
            headerRow.appendChild(th);
        }
        thead.appendChild(headerRow);
        table.appendChild(thead);

        var tbody = document.createElement('tbody');
        for (var itemIndex = 0; itemIndex < items.length; itemIndex += 1) {
            tbody.appendChild(buildSendRunsTableRow(items[itemIndex]));
        }
        table.appendChild(tbody);

        tableArea.appendChild(table);
    }

    /**
     * 테이블 영역에 에러 박스를 그린다 (fetch 실패 시).
     */
    function renderSendRunsError(tableArea, message) {
        tableArea.innerHTML = '';
        var box = document.createElement('div');
        box.className = 'admin-flash admin-flash--error';
        box.setAttribute('role', 'alert');
        box.textContent = '발송 이력 조회 실패: ' + message;
        tableArea.appendChild(box);
    }

    /**
     * 「발송 이력」 섹션을 초기화한다.
     *
     * - 페이지 로드 시 GET /api/admin/email/send-runs?status=all&limit=50.
     * - status 필터 select 변경 시 즉시 재조회.
     * - 새로고침 버튼 클릭 시 재조회.
     * - 'email-test-send-completed' window event 청취 시 자동 재조회
     *   (섹션 2 의 performTestSend 가 완료 직후 dispatch 함).
     *
     * 페이지에 요소가 없으면 즉시 반환 (다른 탭에서 admin_email.js 가 잘못
     * 로드된 경우 안전).
     */
    function initSendRunsSection() {
        var filterSelect = document.getElementById('send-runs-status-filter');
        if (!filterSelect) {
            return;
        }
        var refreshButton = document.getElementById('send-runs-refresh-button');
        var tableArea = document.getElementById('send-runs-table-area');

        filterSelect.addEventListener('change', loadSendRuns);
        refreshButton.addEventListener('click', loadSendRuns);
        // 섹션 2 가 완료 직후 dispatch 하는 신호 청취 (성공/실패 무관).
        window.addEventListener('email-test-send-completed', loadSendRuns);

        // 페이지 진입 시 즉시 최초 로드.
        loadSendRuns();

        /**
         * 현재 필터 값으로 GET 호출하고 테이블을 갱신한다.
         */
        function loadSendRuns() {
            var statusValue = filterSelect.value || 'all';
            var url =
                SEND_RUNS_URL +
                '?status=' + encodeURIComponent(statusValue) +
                '&limit=' + SEND_RUNS_LIMIT;

            // 다중 클릭 / 변경 방지용 disable.
            refreshButton.disabled = true;
            filterSelect.disabled = true;

            fetch(url, {
                method: 'GET',
                credentials: 'same-origin',
                headers: { 'Accept': 'application/json' }
            })
                .then(parseJsonResponse)
                .then(function (result) {
                    if (!result.resp.ok) {
                        throw new Error(
                            extractErrorMessage(result.resp, result.body)
                        );
                    }
                    var items = (result.body && result.body.items) || [];
                    renderSendRunsTable(tableArea, items);
                })
                .catch(function (error) {
                    renderSendRunsError(
                        tableArea, error.message || String(error)
                    );
                })
                .then(function () {
                    refreshButton.disabled = false;
                    filterSelect.disabled = false;
                });
        }
    }

    // ──────────────────────────────────────────────────────────
    // DOMContentLoaded — 페이지 전체 진입점
    // ──────────────────────────────────────────────────────────

    document.addEventListener('DOMContentLoaded', function () {
        initSettingsSection();
        initTestSendSection();
        initSendRunsSection();
    });
})();
