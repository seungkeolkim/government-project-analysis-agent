// 관리자 「시스템 재시작」 탭 인터랙션 (task 00161-2).
//
// 흐름:
//   1. '지금 재시작' 버튼 클릭 → window.confirm 1회.
//   2. POST /admin/system/restart (00161-1 endpoint) — same-origin 이므로
//      ensure_same_origin 통과. 200 수신 시 버튼을 잠그고 '재시작 중…' 표시.
//   3. /healthz 를 일정 간격으로 폴링한다. 자기 자신(웹 컨테이너)이 재시작되는
//      동안 fetch 는 네트워크 에러 또는 비 2xx 로 실패하는데, 이 '다운 구간' 을
//      정상 흐름으로 처리해야 한다. 즉 '한동안 실패(다운) → 다시 200' 패턴을
//      감지해 새 인스턴스 기동으로 판단하고 location.reload() 한다.
//      (POST 직후 1초 sleep 동안은 기존 인스턴스가 아직 200 을 주므로,
//       다운을 한 번도 보지 못한 상태의 200 으로는 새로고침하지 않는다.)
//   4. 폴링에 최대 시도 횟수를 두고, 한계를 넘으면 수동 새로고침을 안내한다.
//
// 외부 의존성 없음 — vanilla JS ES5+ 호환. then 체인으로 통일해 admin_email.js /
// progress.js 와 같은 톤을 유지한다. 모든 fetch 는 credentials: 'same-origin'.
(function () {
    'use strict';

    // ──────────────────────────────────────────────────────────
    // 상수
    // ──────────────────────────────────────────────────────────

    // 00161-1 에서 추가된 셀프 재시작 endpoint.
    var RESTART_ENDPOINT = '/admin/system/restart';

    // 00161-1 에서 추가된 폴링용 경량 health endpoint (인증 불필요).
    var HEALTH_ENDPOINT = '/healthz';

    // health 폴링 간격(ms). 너무 짧으면 다운 구간 동안 요청이 쌓이고, 너무 길면
    // 재기동 감지가 늦다. 2초로 둔다.
    var POLL_INTERVAL_MS = 2000;

    // 최대 폴링 시도 횟수. 2초 간격 × 90회 = 약 3분. 컨테이너 재기동이 이 시간
    // 안에 끝나지 않으면 수동 새로고침을 안내한다.
    var MAX_POLL_ATTEMPTS = 90;

    // ──────────────────────────────────────────────────────────
    // 상태 표시 헬퍼
    // ──────────────────────────────────────────────────────────

    /**
     * 재시작 진행 상태 박스를 그린다.
     *
     * 기존 admin-flash 스타일을 재사용한다. 같은 영역을 매번 비우고 다시 그려
     * 항상 최신 상태 한 줄만 보이게 한다.
     *
     * @param {HTMLElement} area 상태를 그릴 컨테이너(#restart-status-area).
     * @param {string} levelClass admin-flash 변형 suffix ('success'|'error'|'warning').
     * @param {string} message 사용자에게 보여 줄 문구.
     * @param {boolean} [showManualReload] true 면 수동 새로고침 안내를 함께 노출.
     */
    function showStatus(area, levelClass, message, showManualReload) {
        if (!area) {
            return;
        }
        area.innerHTML = '';

        var box = document.createElement('div');
        box.className = 'admin-flash admin-flash--' + levelClass;
        box.setAttribute('role', 'status');
        box.textContent = message;
        area.appendChild(box);

        if (showManualReload) {
            var hint = document.createElement('p');
            hint.className = 'admin-state__muted';
            hint.textContent = '자동 새로고침이 되지 않으면 잠시 후 페이지를 직접 새로고침해주세요.';
            area.appendChild(hint);
        }
    }

    // ──────────────────────────────────────────────────────────
    // 재시작 트리거 + health 폴링
    // ──────────────────────────────────────────────────────────

    /**
     * /healthz 폴링을 시작한다.
     *
     * 새 인스턴스 기동 판단 규칙: '다운 구간(fetch 실패 또는 비 2xx)을 한 번이라도
     * 본 뒤' 다시 200 이 오면 재시작 완료로 보고 새로고침한다. 다운을 보기 전의
     * 200 은 아직 기존 인스턴스가 응답 중인 것이므로 무시한다.
     *
     * @param {HTMLElement} statusArea 상태 표시 영역.
     */
    function startHealthPolling(statusArea) {
        var attempts = 0;
        // 컨테이너가 내려가 health 가 실패한 구간을 한 번이라도 관측했는지 여부.
        var observedDowntime = false;

        function scheduleNext() {
            window.setTimeout(poll, POLL_INTERVAL_MS);
        }

        function poll() {
            attempts += 1;
            if (attempts > MAX_POLL_ATTEMPTS) {
                showStatus(
                    statusArea,
                    'error',
                    '재시작 완료를 확인하지 못했습니다. 컨테이너 상태를 확인해주세요.',
                    true
                );
                return;
            }

            fetch(HEALTH_ENDPOINT, {
                method: 'GET',
                credentials: 'same-origin',
                cache: 'no-store',
                headers: { 'Accept': 'application/json' }
            })
                .then(function (resp) {
                    if (resp.ok) {
                        if (observedDowntime) {
                            // 다운을 본 뒤 다시 200 → 새 인스턴스 기동 완료.
                            showStatus(
                                statusArea,
                                'success',
                                '재시작이 완료되었습니다. 페이지를 새로고침합니다…'
                            );
                            window.location.reload();
                            return;
                        }
                        // 아직 기존 인스턴스가 응답 중 — 다운 구간을 기다린다.
                        scheduleNext();
                    } else {
                        // 비 2xx 도 정상 응답이 아니므로 다운 구간으로 본다.
                        observedDowntime = true;
                        scheduleNext();
                    }
                })
                .catch(function () {
                    // fetch 실패 = 컨테이너가 내려간 다운 구간. 정상 흐름으로 처리.
                    observedDowntime = true;
                    scheduleNext();
                });
        }

        poll();
    }

    /**
     * 재시작 요청을 보내고, 200 수신 시 health 폴링으로 넘어간다.
     *
     * @param {HTMLButtonElement} button 재시작 버튼.
     * @param {HTMLElement} statusArea 상태 표시 영역.
     */
    function triggerRestart(button, statusArea) {
        button.disabled = true;
        showStatus(statusArea, 'success', '재시작 요청을 보내는 중…');

        fetch(RESTART_ENDPOINT, {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Accept': 'application/json' }
        })
            .then(function (resp) {
                if (!resp.ok) {
                    return resp.text().then(function (text) {
                        throw new Error('HTTP ' + resp.status + ' ' + (text || resp.statusText));
                    });
                }
                // 200 — 재시작 명령이 전달됐다. 버튼은 잠근 채로 폴링 시작.
                showStatus(
                    statusArea,
                    'success',
                    '재시작 중… 새 인스턴스가 기동되면 자동으로 새로고침됩니다.'
                );
                startHealthPolling(statusArea);
            })
            .catch(function (error) {
                // 요청 자체가 실패(권한/네트워크/서버 에러). 버튼을 되살린다.
                button.disabled = false;
                showStatus(
                    statusArea,
                    'error',
                    '재시작 요청에 실패했습니다: ' + (error.message || error)
                );
            });
    }

    // ──────────────────────────────────────────────────────────
    // 초기화
    // ──────────────────────────────────────────────────────────

    /**
     * 버튼에 클릭 핸들러를 건다. 클릭 시 confirm 1회 후 재시작을 트리거한다.
     * scrape_running 으로 서버에서 disabled 된 버튼은 클릭되지 않는다.
     */
    function init() {
        var button = document.getElementById('restart-button');
        if (!button) {
            return;
        }
        var statusArea = document.getElementById('restart-status-area');

        button.addEventListener('click', function () {
            var confirmed = window.confirm(
                '지금 iris-agent-web 컨테이너를 재시작할까요?\n' +
                '재시작 동안 잠시 접속이 끊기며, 완료되면 자동으로 새로고침됩니다.'
            );
            if (!confirmed) {
                return;
            }
            triggerRestart(button, statusArea);
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
