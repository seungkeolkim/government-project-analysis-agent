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
    // DOMContentLoaded — 페이지 전체 진입점
    // ──────────────────────────────────────────────────────────

    document.addEventListener('DOMContentLoaded', function () {
        initSettingsSection();
        // 후속 subtask 가 여기에 initTestSendSection() / initSendRunsSection() 호출 추가.
    });
})();
