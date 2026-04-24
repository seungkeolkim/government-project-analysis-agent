// 체크박스 + bulk 읽음/안읽음 처리 (Phase 3a / 00035-5)
// Jinja2 변수 없이 순수 JS 로만 동작한다.
// 필터 파라미터는 #bulk-toolbar 의 data-* 속성으로 수신한다.
// 목록 페이지 이외에서는 #bulk-toolbar 가 없으므로 초기화 직후 반환된다.
(function () {
    'use strict';

    var toolbar = document.getElementById('bulk-toolbar');
    if (!toolbar) { return; }

    var checkAllPage = document.getElementById('check-all-page');
    var bulkReadBtn = document.getElementById('bulk-read-btn');
    var bulkUnreadBtn = document.getElementById('bulk-unread-btn');
    var bulkClearBtn = document.getElementById('bulk-clear-btn');
    var bulkCountLabel = document.getElementById('bulk-count-label');
    var bulkAllFilterWrap = document.getElementById('bulk-all-filter-wrap');
    var bulkAllFilterLink = document.getElementById('bulk-all-filter-link');
    var bulkAllFilterSelected = document.getElementById('bulk-all-filter-selected');
    var bulkDeselectFilter = document.getElementById('bulk-deselect-filter');

    // 현재 선택된 announcement id 집합.
    var selectedIds = new Set();

    // true 이면 "필터 전체 선택" 모드. ids 목록 대신 filter body 를 전송한다.
    var filterAllMode = false;

    // 현재 필터 파라미터 (toolbar data-* 속성에서 읽음 — Jinja2 script block 금지).
    var filterStatus = toolbar.dataset.status || '';
    var filterSearch = toolbar.dataset.search || '';
    var filterSource = toolbar.dataset.source || '';

    // ── 내부 헬퍼 ──────────────────────────────────────────────

    function getRowCheckboxes() {
        return document.querySelectorAll('.row-check');
    }

    /**
     * 현재 선택 상태에 따라 toolbar 가시성과 헤더 체크박스 상태를 갱신한다.
     * 이 함수는 선택 상태가 바뀔 때마다 호출된다.
     */
    function updateUI() {
        var anySelected = filterAllMode || selectedIds.size > 0;
        toolbar.style.display = anySelected ? '' : 'none';

        // 표시할 선택 건수: 필터 전체 모드면 data-total, 아니면 selectedIds.size.
        var displayCount = filterAllMode
            ? parseInt(toolbar.dataset.total, 10)
            : selectedIds.size;
        bulkCountLabel.textContent = displayCount + '개 선택';

        // 헤더 체크박스의 checked/indeterminate 상태 산출.
        var allChecks = getRowCheckboxes();
        var checkedCount = 0;
        allChecks.forEach(function (cb) { if (cb.checked) { checkedCount++; } });
        var allPageSelected = allChecks.length > 0 && checkedCount === allChecks.length;

        if (checkAllPage) {
            if (checkedCount === 0) {
                checkAllPage.checked = false;
                checkAllPage.indeterminate = false;
            } else if (allPageSelected) {
                checkAllPage.checked = true;
                checkAllPage.indeterminate = false;
            } else {
                // 일부만 선택: indeterminate 표시.
                checkAllPage.checked = false;
                checkAllPage.indeterminate = true;
            }
        }

        // "필터 전체 선택" 링크: 페이지 전체가 선택됐을 때만 노출.
        if (filterAllMode) {
            // 필터 전체 선택 활성 상태: "M건 선택됨 · 해제" 표시.
            bulkAllFilterWrap.style.display = '';
            bulkAllFilterLink.style.display = 'none';
            bulkAllFilterSelected.style.display = '';
        } else if (allPageSelected) {
            // 페이지 전체 선택 완료: "필터 전체 M건 선택" 링크 표시.
            bulkAllFilterWrap.style.display = '';
            bulkAllFilterLink.style.display = '';
            bulkAllFilterSelected.style.display = 'none';
        } else {
            // 부분 선택 또는 미선택: 링크 영역 숨김.
            bulkAllFilterWrap.style.display = 'none';
        }
    }

    // ── 이벤트 핸들러 ──────────────────────────────────────────

    // 개별 체크박스 변경: 이벤트 위임으로 처리한다.
    document.addEventListener('change', function (e) {
        if (!e.target.classList.contains('row-check')) { return; }
        var id = parseInt(e.target.value, 10);
        if (e.target.checked) {
            selectedIds.add(id);
        } else {
            selectedIds.delete(id);
            // 개별 해제 시 필터 전체 모드도 해제한다.
            filterAllMode = false;
        }
        updateUI();
    });

    // 헤더 "현재 페이지 전체 선택" 체크박스.
    if (checkAllPage) {
        checkAllPage.addEventListener('change', function () {
            var checks = getRowCheckboxes();
            checks.forEach(function (cb) {
                cb.checked = checkAllPage.checked;
                var id = parseInt(cb.value, 10);
                if (checkAllPage.checked) {
                    selectedIds.add(id);
                } else {
                    selectedIds.delete(id);
                }
            });
            // 헤더 해제 시 필터 전체 모드도 초기화.
            if (!checkAllPage.checked) {
                filterAllMode = false;
            }
            updateUI();
        });
    }

    // "현재 필터 결과 전체 M건 선택" 링크.
    if (bulkAllFilterLink) {
        bulkAllFilterLink.addEventListener('click', function (e) {
            e.preventDefault();
            filterAllMode = true;
            updateUI();
        });
    }

    // "전체 선택 해제" 링크.
    if (bulkDeselectFilter) {
        bulkDeselectFilter.addEventListener('click', function (e) {
            e.preventDefault();
            filterAllMode = false;
            // 페이지 선택 상태는 유지한다.
            updateUI();
        });
    }

    // "선택 해제" 버튼: 모든 체크박스와 selectedIds 를 초기화한다.
    if (bulkClearBtn) {
        bulkClearBtn.addEventListener('click', function () {
            selectedIds.clear();
            filterAllMode = false;
            getRowCheckboxes().forEach(function (cb) { cb.checked = false; });
            if (checkAllPage) {
                checkAllPage.checked = false;
                checkAllPage.indeterminate = false;
            }
            updateUI();
        });
    }

    // ── bulk fetch ─────────────────────────────────────────────

    /**
     * bulk-mark-read / bulk-mark-unread 엔드포인트에 요청을 보낸다.
     *
     * @param {string} endpoint  '/announcements/bulk-mark-read' 등
     * @param {boolean} isRead   읽음=true / 안읽음=false (ids 모드 인플레이스 갱신에 사용)
     */
    function sendBulk(endpoint, isRead) {
        var body;
        if (filterAllMode) {
            // 필터 모드: 현재 URL 필터 파라미터를 그대로 전송한다.
            body = {
                mode: 'filter',
                filter: {
                    status: filterStatus || null,
                    search: filterSearch || null,
                    source: filterSource || null,
                },
            };
        } else {
            // ids 모드: 현재 선택된 announcement id 목록을 전송한다.
            body = {
                mode: 'ids',
                ids: Array.from(selectedIds),
            };
        }

        fetch(endpoint, {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        })
        .then(function (resp) {
            if (!resp.ok) {
                return resp.json().catch(function () { return {}; }).then(function (data) {
                    alert(data.detail || '처리 실패. 다시 시도해 주세요.');
                });
            }
            if (filterAllMode) {
                // 필터 전체 모드: 전체 재로드로 최신 상태 반영.
                location.reload();
            } else {
                // ids 모드: 영향받은 row 의 타이틀 링크 클래스를 인플레이스 갱신.
                selectedIds.forEach(function (id) {
                    var cb = document.querySelector('.row-check[value="' + id + '"]');
                    if (!cb) { return; }
                    var row = cb.closest('tr');
                    if (!row) { return; }
                    var link = row.querySelector('.announcement-title-link');
                    if (!link) { return; }
                    if (isRead) {
                        link.classList.remove('announcement-title-link--unread');
                        link.classList.add('announcement-title-link--read');
                    } else {
                        link.classList.remove('announcement-title-link--read');
                        link.classList.add('announcement-title-link--unread');
                    }
                });
                // 처리 완료 후 선택 해제.
                if (bulkClearBtn) { bulkClearBtn.click(); }
            }
        })
        .catch(function () {
            alert('네트워크 오류. 다시 시도해 주세요.');
        });
    }

    if (bulkReadBtn) {
        bulkReadBtn.addEventListener('click', function () {
            sendBulk('/announcements/bulk-mark-read', true);
        });
    }

    if (bulkUnreadBtn) {
        bulkUnreadBtn.addEventListener('click', function () {
            sendBulk('/announcements/bulk-mark-unread', false);
        });
    }
}());
