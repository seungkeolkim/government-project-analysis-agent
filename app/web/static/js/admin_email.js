// 관리자 「메일 발송」 탭 인터랙션 (Phase A-1 / task 00104-11~13,
// Phase A-3 / task 00125-9).
//
// 본 파일은 5 개 섹션의 init 함수를 들고 있다 — 각 섹션은 별도 subtask 에서
// 단계적으로 도입됐다.
//   - initSettingsSection       — 메일 설정 form (task 00104-11)
//   - initTestSendSection       — 테스트 발송 (task 00104-12)
//   - initSendRunsSection       — 발송 이력 (task 00104-13)
//   - initDailyReportSection    — Daily Report 카드 (task 00125-9)
//   - initDailyReportRunsSection — Daily Report 발송 이력 (task 00125-9)
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

        var sendEnabledCheckbox = document.getElementById('email-send-enabled');
        var tenantIdInput = document.getElementById('email-tenant-id');
        var clientIdInput = document.getElementById('email-client-id');
        var clientSecretInput = document.getElementById('email-client-secret');
        var clientSecretToggle = document.getElementById('email-client-secret-toggle');
        var senderAddressInput = document.getElementById('email-sender-address');
        var fromDisplayNameInput = document.getElementById('email-from-display-name');
        var maxRetryCountInput = document.getElementById('email-max-retry-count');
        var publicBaseUrlInput = document.getElementById('email-public-base-url');
        var saveButton = document.getElementById('email-settings-save');

        // ── 초기 로드 ─────────────────────────────────────────
        loadSettings();

        // ── 메일 전송 기능 활성화 토글 — change 즉시 PUT /settings ──
        if (sendEnabledCheckbox) {
            sendEnabledCheckbox.addEventListener('change', function () {
                saveSendEnabled(sendEnabledCheckbox.checked);
            });
        }

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
            // 메일 전송 기능 활성화 토글 상태 갱신.
            if (sendEnabledCheckbox) {
                sendEnabledCheckbox.checked = !!(settings && settings.send_enabled);
            }
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
            // public_base_url — 메일 포워딩 공고 링크의 Base URL.
            publicBaseUrlInput.value = settings.public_base_url || '';
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
                send_enabled: !!(sendEnabledCheckbox && sendEnabledCheckbox.checked),
                m365: {
                    tenant_id: tenantIdInput.value.trim(),
                    client_id: clientIdInput.value.trim(),
                    sender_address: senderAddressInput.value.trim()
                },
                from_display_name: fromDisplayNameInput.value.trim(),
                max_retry_count: parseInt(maxRetryCountInput.value, 10),
                public_base_url: publicBaseUrlInput.value.trim()
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

        /**
         * 메일 전송 기능 활성화 토글 변경 시 즉시 PUT /settings 로 저장한다.
         *
         * 전체 설정 form 값을 수집해 send_enabled 만 교체한 뒤 PUT 한다.
         * 실패 시 토글 상태를 변경 전 값으로 되돌리고 error flash 를 표시한다.
         *
         * @param {boolean} newEnabledValue 저장할 새 활성화 여부.
         */
        function saveSendEnabled(newEnabledValue) {
            clearFlash();
            if (sendEnabledCheckbox) {
                sendEnabledCheckbox.disabled = true;
            }

            var inEditMode =
                clientSecretToggle.getAttribute('aria-pressed') === 'true';

            var requestBody = {
                send_enabled: newEnabledValue,
                m365: {
                    tenant_id: tenantIdInput.value.trim(),
                    client_id: clientIdInput.value.trim(),
                    sender_address: senderAddressInput.value.trim()
                },
                from_display_name: fromDisplayNameInput.value.trim(),
                max_retry_count: parseInt(maxRetryCountInput.value, 10),
                public_base_url: publicBaseUrlInput.value.trim()
            };

            // client_secret 은 토글 ON + 입력값이 있을 때만 포함 (기존 saveSettings 와 동일 규칙).
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
                    applySettingsToForm(result.body || {});
                    var stateText = newEnabledValue ? '활성화' : '비활성화';
                    showFlash('success', '메일 전송 기능이 ' + stateText + '되었습니다.');
                })
                .catch(function (error) {
                    // PUT 실패 시 토글을 이전 상태로 복원.
                    if (sendEnabledCheckbox) {
                        sendEnabledCheckbox.checked = !newEnabledValue;
                    }
                    showFlash(
                        'error',
                        '메일 발송 설정 저장 실패: ' + (error.message || error)
                    );
                })
                .then(function () {
                    if (sendEnabledCheckbox) {
                        sendEnabledCheckbox.disabled = false;
                    }
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
    // 섹션 4: Daily Report 카드 (Phase A-3 / task 00125-9)
    // ──────────────────────────────────────────────────────────

    var DAILY_REPORT_SETTINGS_URL = '/api/admin/email/daily-report/settings';
    var DAILY_REPORT_TEST_SEND_URL = '/api/admin/email/daily-report/test-send';
    var DAILY_REPORT_SEND_NOW_URL = '/api/admin/email/daily-report/send-now';

    /**
     * Daily Report 발송 status 코드를 아이콘 + 한글 라벨로 변환한다.
     *
     * @param {string} status success / partial / failed / skipped / in_progress.
     * @returns {string} 아이콘 + 라벨 (예: '✅ 성공'). 미지원 값은 raw 그대로.
     */
    function formatDailyReportStatus(status) {
        if (status === 'success') {
            return '✅ 성공';
        }
        if (status === 'partial') {
            return '⚠️ 부분 성공';
        }
        if (status === 'failed') {
            return '❌ 실패';
        }
        if (status === 'skipped') {
            return '⏭ 건너뜀';
        }
        if (status === 'in_progress') {
            return '⏳ 진행 중';
        }
        return status ? String(status) : '-';
    }

    /**
     * Daily Report 트리거 코드를 한글 라벨로 변환한다.
     *
     * @param {string} trigger scheduled / manual_admin / manual_test.
     * @returns {string} 한글 라벨. 미지원 값은 raw 그대로.
     */
    function formatDailyReportTrigger(trigger) {
        if (trigger === 'scheduled') {
            return '예약';
        }
        if (trigger === 'manual_admin') {
            return '지금 발송';
        }
        if (trigger === 'manual_test') {
            return '테스트';
        }
        return trigger ? String(trigger) : '-';
    }

    /**
     * ISO-8601 datetime 문자열을 KST 날짜(YYYY-MM-DD) 로 변환한다.
     *
     * 누적 구간 컬럼은 시각까지 표시하면 셀이 길어지므로 날짜만 노출한다.
     *
     * @param {string|null|undefined} isoString ISO-8601 datetime 또는 falsy.
     * @returns {string} 'YYYY-MM-DD' 또는 빈 문자열 (falsy 입력).
     */
    function formatDateKst(isoString) {
        if (!isoString) {
            return '';
        }
        var dateValue = new Date(isoString);
        if (isNaN(dateValue.getTime())) {
            return String(isoString);
        }
        return dateValue.toLocaleDateString('en-CA', {
            timeZone: 'Asia/Seoul',
            year: 'numeric',
            month: '2-digit',
            day: '2-digit'
        });
    }

    /**
     * window 에 'daily-report-send-completed' custom event 를 dispatch 한다.
     * Daily Report 발송 이력 섹션이 이 신호를 청취해 테이블을 자동 재로드한다.
     */
    function dispatchDailyReportSendCompletedEvent() {
        try {
            window.dispatchEvent(new CustomEvent('daily-report-send-completed'));
        } catch (dispatchError) {
            // 옛 브라우저 — silent. 새로고침 버튼으로 수동 갱신 가능.
        }
    }

    /**
     * 「Daily Report」 카드를 초기화한다.
     *
     * - 페이지 로드 시 GET /daily-report/settings 로 현재 값을 받아 카드를 채운다.
     * - [저장] 클릭 → PUT /daily-report/settings (enabled / cron / test_recipient).
     * - [테스트 발송] 클릭 → POST /daily-report/test-send (현재 받는 사람 입력값).
     * - [지금 발송] 클릭 → window.confirm 후 POST /daily-report/send-now.
     *
     * 페이지에 form 요소가 없으면 즉시 반환 — 멱등 안전.
     */
    function initDailyReportSection() {
        var form = document.getElementById('daily-report-settings-form');
        if (!form) {
            return;
        }

        var enabledCheckbox = document.getElementById('daily-report-enabled');
        var cronInput = document.getElementById('daily-report-cron');
        var nextRunSpan = document.getElementById('daily-report-next-run');
        var lastSentSpan = document.getElementById('daily-report-last-sent');
        var recipientsSummary =
            document.getElementById('daily-report-recipients-summary');
        var recipientsDetail =
            document.getElementById('daily-report-recipients-detail');
        var testRecipientInput =
            document.getElementById('daily-report-test-recipient');
        var testSendButton =
            document.getElementById('daily-report-test-send-button');
        var sendNowButton =
            document.getElementById('daily-report-send-now-button');
        var saveButton = document.getElementById('daily-report-settings-save');
        var flashArea = document.getElementById('daily-report-flash-area');
        var resultArea = document.getElementById('daily-report-result-area');

        // 가장 최근에 로드한 settings — 「지금 발송」 confirm 메시지에 수신자
        // 명단을 보여주기 위해 보관한다.
        var latestSettings = null;

        // ── 초기 로드 ─────────────────────────────────────────
        loadDailyReportSettings();

        // ── 이벤트 바인딩 ─────────────────────────────────────
        form.addEventListener('submit', function (event) {
            event.preventDefault();
            saveDailyReportSettings();
        });
        testSendButton.addEventListener('click', function () {
            performDailyReportTestSend();
        });
        sendNowButton.addEventListener('click', function () {
            performDailyReportSendNow();
        });

        /**
         * 카드 flash 영역에 success / error 박스를 그린다.
         *
         * @param {'success'|'error'} kind 박스 종류.
         * @param {string} message 한글 메시지.
         */
        function showDailyReportFlash(kind, message) {
            if (!flashArea) {
                return;
            }
            flashArea.innerHTML = '';
            var box = document.createElement('div');
            box.className = 'admin-flash admin-flash--' + kind;
            box.setAttribute('role', kind === 'error' ? 'alert' : 'status');
            box.textContent = message;
            flashArea.appendChild(box);
        }

        /**
         * 카드 flash 영역을 비운다 (새 요청 시작 시 호출).
         */
        function clearDailyReportFlash() {
            if (flashArea) {
                flashArea.innerHTML = '';
            }
        }

        /**
         * 테스트/지금 발송 결과 박스를 그린다. 여러 줄은 <br> 로 분리한다.
         *
         * @param {'success'|'error'} kind admin-flash--success / --error.
         * @param {string[]} lines 사용자에게 보여 줄 한글 메시지 줄들.
         */
        function showDailyReportResult(kind, lines) {
            if (!resultArea) {
                return;
            }
            resultArea.innerHTML = '';
            var box = document.createElement('div');
            box.className = 'admin-flash admin-flash--' + kind;
            box.setAttribute('role', kind === 'error' ? 'alert' : 'status');
            for (var index = 0; index < lines.length; index += 1) {
                if (index > 0) {
                    box.appendChild(document.createElement('br'));
                }
                box.appendChild(document.createTextNode(lines[index]));
            }
            resultArea.appendChild(box);
        }

        /**
         * settings 응답을 카드 입력/표시 요소에 반영한다.
         *
         * @param {object} settings GET / PUT 응답 형식의 DailyReportSettingsOut.
         */
        function applyDailyReportSettings(settings) {
            latestSettings = settings || {};

            enabledCheckbox.checked = !!latestSettings.enabled;
            cronInput.value = latestSettings.cron_expression || '';
            testRecipientInput.value = latestSettings.test_recipient || '';

            // 다음 실행 예측 — next_run_at 이 있으면 KST 표시, 없으면 안내.
            nextRunSpan.textContent = latestSettings.next_run_at
                ? formatDateTimeKst(latestSettings.next_run_at)
                : '— (비활성 또는 미등록)';

            // 마지막 발송 — last_sent_at 이 NULL 이면 첫 발송 전.
            lastSentSpan.textContent = latestSettings.last_sent_at
                ? formatDateTimeKst(latestSettings.last_sent_at)
                : '— (아직 발송된 적 없음)';

            renderRecipientsSummary(latestSettings);
            renderRecipientsDetail(latestSettings);
        }

        /**
         * 수신자 요약 줄 — 전체 사용자 수 / 발송 대상 수 / 미설정·수신거부 경고.
         *
         * @param {object} settings DailyReportSettingsOut.
         */
        function renderRecipientsSummary(settings) {
            recipientsSummary.innerHTML = '';

            var recipientList = Array.isArray(settings.recipients)
                ? settings.recipients
                : [];
            var eligibleCount =
                typeof settings.recipient_count_eligible === 'number'
                    ? settings.recipient_count_eligible
                    : 0;
            var withoutEmailCount =
                typeof settings.recipient_count_without_email === 'number'
                    ? settings.recipient_count_without_email
                    : 0;
            var unsubscribedCount =
                typeof settings.recipient_count_unsubscribed === 'number'
                    ? settings.recipient_count_unsubscribed
                    : 0;

            var summaryLine = document.createElement('p');
            summaryLine.className = 'admin-state__muted';
            summaryLine.textContent =
                '수신자: 전체 사용자 ' + recipientList.length + '명 중 발송 대상 ' +
                eligibleCount + '명';
            recipientsSummary.appendChild(summaryLine);

            // 미설정 / 수신거부 사용자가 있으면 노란 경고 박스.
            if (withoutEmailCount > 0 || unsubscribedCount > 0) {
                var warningParts = [];
                if (withoutEmailCount > 0) {
                    warningParts.push('이메일 미설정 ' + withoutEmailCount + '명');
                }
                if (unsubscribedCount > 0) {
                    warningParts.push('수신 거부 ' + unsubscribedCount + '명');
                }
                var warningBox = document.createElement('div');
                warningBox.className = 'admin-flash admin-flash--warning';
                warningBox.setAttribute('role', 'status');
                warningBox.textContent =
                    '⚠️ ' + warningParts.join(' · ') +
                    ' — 해당 사용자는 발송 대상에서 제외됩니다.';
                recipientsSummary.appendChild(warningBox);
            }
        }

        /**
         * 수신자 목록 expand 영역에 상세 테이블을 그린다.
         *
         * @param {object} settings DailyReportSettingsOut.
         */
        function renderRecipientsDetail(settings) {
            recipientsDetail.innerHTML = '';

            var recipientList = Array.isArray(settings.recipients)
                ? settings.recipients
                : [];
            if (recipientList.length === 0) {
                var emptyText = document.createElement('p');
                emptyText.className = 'admin-state__muted';
                emptyText.textContent = '사용자가 없습니다.';
                recipientsDetail.appendChild(emptyText);
                return;
            }

            var table = document.createElement('table');
            table.className = 'daily-report-recipients__table';

            var thead = document.createElement('thead');
            var headerRow = document.createElement('tr');
            ['사용자', '이메일', '수신 동의', '발송 대상'].forEach(
                function (headerText) {
                    var th = document.createElement('th');
                    th.textContent = headerText;
                    headerRow.appendChild(th);
                }
            );
            thead.appendChild(headerRow);
            table.appendChild(thead);

            var tbody = document.createElement('tbody');
            recipientList.forEach(function (recipient) {
                var row = document.createElement('tr');
                // 발송 제외 사용자 행은 흐리게 표시.
                if (!recipient.eligible) {
                    row.className = 'daily-report-recipients__row--excluded';
                }

                var usernameTd = document.createElement('td');
                usernameTd.textContent = recipient.username || '';
                row.appendChild(usernameTd);

                var emailTd = document.createElement('td');
                emailTd.textContent = recipient.email || '(미설정)';
                row.appendChild(emailTd);

                var subscribedTd = document.createElement('td');
                subscribedTd.textContent =
                    recipient.email_subscribed ? '동의' : '거부';
                row.appendChild(subscribedTd);

                var eligibleTd = document.createElement('td');
                eligibleTd.textContent = recipient.eligible ? '✅ 포함' : '제외';
                row.appendChild(eligibleTd);

                tbody.appendChild(row);
            });
            table.appendChild(tbody);
            recipientsDetail.appendChild(table);
        }

        /**
         * GET /daily-report/settings — 페이지 로드 시 또는 저장 직후 호출.
         */
        function loadDailyReportSettings() {
            clearDailyReportFlash();
            saveButton.disabled = true;
            fetch(DAILY_REPORT_SETTINGS_URL, {
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
                    applyDailyReportSettings(result.body || {});
                })
                .catch(function (error) {
                    showDailyReportFlash(
                        'error',
                        'Daily Report 설정 조회 실패: ' + (error.message || error)
                    );
                })
                .then(function () {
                    saveButton.disabled = false;
                });
        }

        /**
         * PUT /daily-report/settings — [저장] 버튼 클릭 시 호출.
         *
         * enabled / cron_expression / test_recipient 3종을 저장한다. 저장 성공
         * 시 서버가 register_daily_report_cron_schedule() 로 APScheduler 잡을
         * 등록/제거하며, 응답에 갱신된 next_run_at 까지 포함된다.
         */
        function saveDailyReportSettings() {
            clearDailyReportFlash();
            saveButton.disabled = true;

            var requestBody = {
                enabled: !!enabledCheckbox.checked,
                cron_expression: cronInput.value.trim(),
                test_recipient: testRecipientInput.value.trim()
            };

            fetch(DAILY_REPORT_SETTINGS_URL, {
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
                    applyDailyReportSettings(result.body || {});
                    showDailyReportFlash(
                        'success', 'Daily Report 설정이 저장되었습니다.'
                    );
                })
                .catch(function (error) {
                    showDailyReportFlash(
                        'error', '저장 실패: ' + (error.message || error)
                    );
                })
                .then(function () {
                    saveButton.disabled = false;
                });
        }

        /**
         * POST /daily-report/test-send — [테스트 발송] 버튼 클릭 시 호출.
         *
         * 현재 받는 사람 입력값을 그대로 전송한다. 빈 값이면 서버가 SystemSetting
         * 의 test_recipient 를 fallback 으로 쓴다. trigger=manual_test 이므로
         * last_sent_at 은 갱신되지 않는다.
         */
        function performDailyReportTestSend() {
            if (resultArea) {
                resultArea.innerHTML = '';
            }
            testSendButton.disabled = true;

            var requestBody = {
                recipient: testRecipientInput.value.trim()
            };

            fetch(DAILY_REPORT_TEST_SEND_URL, {
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
                    var body = result.body || {};
                    if (resp.ok) {
                        showDailyReportResult('success', [
                            '테스트 발송 완료 (run_id: ' + body.run_id +
                                ', 상태: ' + formatDailyReportStatus(body.status) +
                                ').',
                            '수신함과 정크메일 폴더를 모두 확인해주세요.'
                        ]);
                    } else {
                        showDailyReportResult('error', [
                            '테스트 발송 실패 (HTTP ' + resp.status + '): ' +
                                extractErrorMessage(resp, body),
                            '아래 「Daily Report 발송 이력」 에서 상세 정보를 확인할 수 있습니다.'
                        ]);
                    }
                })
                .catch(function (error) {
                    showDailyReportResult('error', [
                        '테스트 발송 요청 실패: ' + (error.message || error),
                        '네트워크 연결을 확인하거나 잠시 후 다시 시도해주세요.'
                    ]);
                })
                .then(function () {
                    testSendButton.disabled = false;
                    // 발송 이력 섹션에 자동 새로고침 신호.
                    dispatchDailyReportSendCompletedEvent();
                });
        }

        /**
         * POST /daily-report/send-now — [지금 발송] 클릭 시 호출.
         *
         * 메일 발송은 외부 영향이 큰 액션이라 window.confirm 으로 우발 클릭을
         * 막는다 (디자인 노트 §10). 확인 시 현재 시점 발송 대상 사용자 전원에게
         * 즉시 발송한다 (trigger=manual_admin).
         */
        function performDailyReportSendNow() {
            // confirm 메시지 — 가장 최근 로드한 settings 의 발송 대상 명단 사용.
            var eligibleEmails = [];
            if (latestSettings && Array.isArray(latestSettings.recipients)) {
                latestSettings.recipients.forEach(function (recipient) {
                    if (recipient.eligible && recipient.email) {
                        eligibleEmails.push(recipient.email);
                    }
                });
            }
            var confirmMessage =
                '현재 발송 대상 ' + eligibleEmails.length +
                '명에게 즉시 Daily Report 를 발송합니다. 계속할까요?';
            if (eligibleEmails.length > 0) {
                confirmMessage += '\n\n수신자: ' + eligibleEmails.join(', ');
            }
            if (!window.confirm(confirmMessage)) {
                return;
            }

            if (resultArea) {
                resultArea.innerHTML = '';
            }
            sendNowButton.disabled = true;

            fetch(DAILY_REPORT_SEND_NOW_URL, {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'Accept': 'application/json',
                    'Content-Type': 'application/json'
                }
            })
                .then(parseJsonResponse)
                .then(function (result) {
                    var resp = result.resp;
                    var body = result.body || {};
                    if (resp.ok) {
                        showDailyReportResult('success', [
                            '즉시 발송 완료 (run_id: ' + body.run_id +
                                ', 상태: ' + formatDailyReportStatus(body.status) +
                                ').',
                            '수신자 ' + body.recipient_count + '명 중 성공 ' +
                                body.success_count + '명 / 실패 ' +
                                body.failure_count + '명.'
                        ]);
                        // last_sent_at 이 갱신될 수 있으므로 설정도 다시 로드.
                        loadDailyReportSettings();
                    } else {
                        showDailyReportResult('error', [
                            '즉시 발송 실패 (HTTP ' + resp.status + '): ' +
                                extractErrorMessage(resp, body),
                            '아래 「Daily Report 발송 이력」 에서 상세 정보를 확인할 수 있습니다.'
                        ]);
                    }
                })
                .catch(function (error) {
                    showDailyReportResult('error', [
                        '즉시 발송 요청 실패: ' + (error.message || error),
                        '네트워크 연결을 확인하거나 잠시 후 다시 시도해주세요.'
                    ]);
                })
                .then(function () {
                    sendNowButton.disabled = false;
                    dispatchDailyReportSendCompletedEvent();
                });
        }
    }

    // ──────────────────────────────────────────────────────────
    // 섹션 5: Daily Report 발송 이력 (Phase A-3 / task 00125-9)
    // ──────────────────────────────────────────────────────────

    var DAILY_REPORT_RUNS_URL = '/api/admin/email/daily-report/runs';
    var DAILY_REPORT_RUNS_LIMIT = 50;

    /**
     * EmailDailyReportRun 1 row 의 누적 구간 표시 문자열을 만든다.
     *
     * @param {object} run /daily-report/runs 응답 1건.
     * @returns {string} 'YYYY-MM-DD ~ YYYY-MM-DD' 또는 '-' (구간 정보 없음).
     */
    function buildDailyReportRangeText(run) {
        var fromText = formatDateKst(run.aggregation_from);
        var toText = formatDateKst(run.aggregation_to);
        if (!fromText && !toText) {
            // SKIPPED 등 — 누적 구간이 기록되지 않음.
            return '-';
        }
        return (fromText || '?') + ' ~ ' + (toText || '?');
    }

    /**
     * EmailDailyReportRun 1 row 의 발송 결과 요약 문자열을 만든다.
     *
     * @param {object} run /daily-report/runs 응답 1건.
     * @returns {string} 예: '3건 성공', '2건 성공 / 1건 실패', '-' (skipped).
     */
    function buildDailyReportResultText(run) {
        if (run.status === 'skipped') {
            return '-';
        }
        var successCount =
            typeof run.success_count === 'number' ? run.success_count : 0;
        var failureCount =
            typeof run.failure_count === 'number' ? run.failure_count : 0;
        var resultText = successCount + '건 성공';
        if (failureCount > 0) {
            resultText += ' / ' + failureCount + '건 실패';
        }
        return resultText;
    }

    /**
     * 수신자별 발송 결과(EmailSendRun) status 의 아이콘 표시를 만든다.
     *
     * @param {string} status 'sent' | 'failed' | 그 외.
     * @returns {string} '✅ 성공' | '❌ 실패' | 원본 문자열.
     */
    function buildDailyReportSendStatusText(status) {
        if (status === 'sent') {
            return '✅ 성공';
        }
        if (status === 'failed') {
            return '❌ 실패';
        }
        return status ? String(status) : '-';
    }

    /**
     * 수신자별 발송 결과 표를 만든다 — forward 발송 이력의 sends 표 패턴 재사용.
     *
     * @param {Array} sendRuns /runs/{id}/sends 응답의 items 배열.
     * @returns {HTMLElement} 표 또는 빈 상태 안내 요소.
     */
    function buildDailyReportSendsTable(sendRuns) {
        if (!sendRuns || sendRuns.length === 0) {
            var emptyBox = document.createElement('p');
            emptyBox.className = 'forward-history__sends-empty';
            emptyBox.textContent =
                '수신자별 발송 기록이 없습니다 (건너뜀 또는 발송 전 실패).';
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
            { text: '발송 시각', className: 'forward-history__sends-col-sent-at' }
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

            var recipientTd = document.createElement('td');
            recipientTd.textContent = sendRun.recipient || '-';
            row.appendChild(recipientTd);

            var statusTd = document.createElement('td');
            statusTd.textContent =
                buildDailyReportSendStatusText(sendRun.status);
            row.appendChild(statusTd);

            var attemptTd = document.createElement('td');
            attemptTd.textContent =
                typeof sendRun.attempt_count === 'number'
                    ? String(sendRun.attempt_count) + '회'
                    : '-';
            row.appendChild(attemptTd);

            var errorTd = document.createElement('td');
            errorTd.className = 'forward-history__sends-error';
            errorTd.textContent = sendRun.error_message
                ? sendRun.error_message
                : '-';
            row.appendChild(errorTd);

            var sentAtTd = document.createElement('td');
            sentAtTd.textContent = formatDateTimeKst(sendRun.sent_at) || '-';
            row.appendChild(sentAtTd);

            tbody.appendChild(row);
        });
        table.appendChild(tbody);

        return table;
    }

    /**
     * 발송 이력 1 행의 expand 영역을 펼치거나 접는다.
     *
     * 첫 펼침에서만 GET /runs/{id}/sends 를 호출하고 응답을 DOM 에 캐시한다
     * (발송 결과는 immutable). 호출이 실패하면 캐시하지 않아 다음 펼침에서
     * 재시도된다 — forward_history.js 의 toggleExpand 패턴과 동일.
     *
     * @param {HTMLTableRowElement} mainRow main 행.
     * @param {HTMLTableRowElement} expandRow main 행 바로 아래 hidden 행.
     */
    function toggleDailyReportRunExpand(mainRow, expandRow) {
        var expandCell =
            expandRow.querySelector('.forward-history__expand-cell');
        var toggleButton =
            mainRow.querySelector('.forward-history__toggle');

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

        // 이미 한 번 불러왔으면 캐시된 DOM 을 그대로 보여준다.
        if (expandRow.dataset.loaded === '1') {
            return;
        }

        var runId = mainRow.dataset.runId;
        expandCell.innerHTML = '';
        var loadingBox = document.createElement('p');
        loadingBox.className = 'forward-history__sends-loading';
        loadingBox.textContent = '수신자별 발송 결과를 불러오는 중…';
        expandCell.appendChild(loadingBox);

        fetch(
            DAILY_REPORT_RUNS_URL + '/' + encodeURIComponent(runId) + '/sends',
            {
                method: 'GET',
                credentials: 'same-origin',
                headers: { 'Accept': 'application/json' }
            }
        )
            .then(parseJsonResponse)
            .then(function (result) {
                if (!result.resp.ok) {
                    throw new Error(
                        extractErrorMessage(result.resp, result.body)
                    );
                }
                var items = (result.body && result.body.items) || [];
                expandCell.innerHTML = '';
                expandCell.appendChild(buildDailyReportSendsTable(items));
                // 발송 결과는 immutable — 한 번 성공하면 캐시한다.
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

    /**
     * EmailDailyReportRun 목록을 테이블로 렌더한다. 각 run 은 main 행 + 그
     * 아래 hidden expand 행 2개로 구성되며, main 행 클릭 시 수신자별 발송
     * 결과가 펼쳐진다. 모든 동적 텍스트는 textContent 로 주입해 XSS 를 막는다.
     *
     * @param {HTMLElement} tableArea 테이블을 그릴 컨테이너.
     * @param {Array} runs /daily-report/runs 응답의 items 배열.
     */
    function renderDailyReportRunsTable(tableArea, runs) {
        tableArea.innerHTML = '';

        if (!runs || runs.length === 0) {
            var emptyMessage = document.createElement('p');
            emptyMessage.className = 'admin-state__muted';
            emptyMessage.textContent = 'Daily Report 발송 이력이 없습니다.';
            tableArea.appendChild(emptyMessage);
            return;
        }

        var table = document.createElement('table');
        table.className = 'forward-history__table';

        var thead = document.createElement('thead');
        var headerRow = document.createElement('tr');
        var headers = [
            '시각 (KST)', '트리거', '상태', '구간',
            '스냅샷', '결과', ''
        ];
        headers.forEach(function (headerText) {
            var th = document.createElement('th');
            th.textContent = headerText;
            headerRow.appendChild(th);
        });
        thead.appendChild(headerRow);
        table.appendChild(thead);

        var tbody = document.createElement('tbody');
        runs.forEach(function (run) {
            // ── main 행 ──
            var mainRow = document.createElement('tr');
            mainRow.className = 'forward-history__row';
            mainRow.dataset.runId = String(run.id);

            var timeTd = document.createElement('td');
            timeTd.textContent = formatDateTimeKst(run.started_at) || '-';
            mainRow.appendChild(timeTd);

            var triggerTd = document.createElement('td');
            triggerTd.textContent = formatDailyReportTrigger(run.trigger);
            mainRow.appendChild(triggerTd);

            var statusTd = document.createElement('td');
            statusTd.textContent = formatDailyReportStatus(run.status);
            mainRow.appendChild(statusTd);

            var rangeTd = document.createElement('td');
            rangeTd.textContent = buildDailyReportRangeText(run);
            mainRow.appendChild(rangeTd);

            var snapshotTd = document.createElement('td');
            snapshotTd.textContent =
                typeof run.snapshot_count === 'number'
                    ? String(run.snapshot_count) + '건'
                    : '-';
            mainRow.appendChild(snapshotTd);

            var resultTd = document.createElement('td');
            // 에러 메시지가 있으면 tooltip 으로 전체를 노출.
            if (run.error_message) {
                resultTd.title = run.error_message;
            }
            resultTd.textContent = buildDailyReportResultText(run);
            mainRow.appendChild(resultTd);

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
                toggleDailyReportRunExpand(mainRow, expandRow);
            });

            tbody.appendChild(mainRow);
            tbody.appendChild(expandRow);
        });
        table.appendChild(tbody);

        tableArea.appendChild(table);
    }

    /**
     * 테이블 영역에 에러 박스를 그린다 (fetch 실패 시).
     *
     * @param {HTMLElement} tableArea 테이블 컨테이너.
     * @param {string} message 에러 메시지.
     */
    function renderDailyReportRunsError(tableArea, message) {
        tableArea.innerHTML = '';
        var box = document.createElement('div');
        box.className = 'admin-flash admin-flash--error';
        box.setAttribute('role', 'alert');
        box.textContent = 'Daily Report 발송 이력 조회 실패: ' + message;
        tableArea.appendChild(box);
    }

    /**
     * 「Daily Report 발송 이력」 섹션을 초기화한다.
     *
     * - 페이지 로드 시 GET /daily-report/runs?limit=50.
     * - 새로고침 버튼 클릭 시 재조회.
     * - 'daily-report-send-completed' window event 청취 시 자동 재조회
     *   (Daily Report 카드의 테스트/지금 발송 직후 dispatch 됨).
     *
     * 페이지에 요소가 없으면 즉시 반환 — 멱등 안전.
     */
    function initDailyReportRunsSection() {
        var tableArea =
            document.getElementById('daily-report-runs-table-area');
        if (!tableArea) {
            return;
        }
        var refreshButton =
            document.getElementById('daily-report-runs-refresh-button');

        if (refreshButton) {
            refreshButton.addEventListener('click', loadDailyReportRuns);
        }
        // 카드의 발송 완료 직후 신호 청취 (성공/실패 무관).
        window.addEventListener(
            'daily-report-send-completed', loadDailyReportRuns
        );

        // 페이지 진입 시 즉시 최초 로드.
        loadDailyReportRuns();

        /**
         * GET /daily-report/runs 호출 후 테이블을 갱신한다.
         */
        function loadDailyReportRuns() {
            if (refreshButton) {
                refreshButton.disabled = true;
            }

            fetch(
                DAILY_REPORT_RUNS_URL + '?limit=' + DAILY_REPORT_RUNS_LIMIT,
                {
                    method: 'GET',
                    credentials: 'same-origin',
                    headers: { 'Accept': 'application/json' }
                }
            )
                .then(parseJsonResponse)
                .then(function (result) {
                    if (!result.resp.ok) {
                        throw new Error(
                            extractErrorMessage(result.resp, result.body)
                        );
                    }
                    var items = (result.body && result.body.items) || [];
                    renderDailyReportRunsTable(tableArea, items);
                })
                .catch(function (error) {
                    renderDailyReportRunsError(
                        tableArea, error.message || String(error)
                    );
                })
                .then(function () {
                    if (refreshButton) {
                        refreshButton.disabled = false;
                    }
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
        initDailyReportSection();
        initDailyReportRunsSection();
    });
})();
