// 즐겨찾기 페이지 전용 JS (Phase 3b / 00036-7)
// 폴더 CRUD(추가·이름변경·삭제) + 즐겨찾기 항목 제거.
// 뮤테이션 후 window.location.reload() 로 SSR 재렌더한다.
(function () {
    'use strict';

    var newNameInput = document.getElementById('fav-sidebar-new-name');
    var addRootBtn   = document.getElementById('fav-sidebar-add-root-btn');
    var addSubBtn    = document.getElementById('fav-sidebar-add-sub-btn');
    var sidebarError = document.getElementById('fav-sidebar-error');

    var renameDialog    = document.getElementById('fav-rename-dialog');
    var renameInput     = document.getElementById('fav-rename-input');
    var renameSaveBtn   = document.getElementById('fav-rename-save-btn');
    var renameCancelBtn = document.getElementById('fav-rename-cancel-btn');
    var renameError     = document.getElementById('fav-rename-error');

    var confirmDialog    = document.getElementById('fav-confirm-dialog');
    var confirmTitle     = document.getElementById('fav-confirm-title');
    var confirmMessage   = document.getElementById('fav-confirm-message');
    var confirmOkBtn     = document.getElementById('fav-confirm-ok-btn');
    var confirmCancelBtn = document.getElementById('fav-confirm-cancel-btn');

    // 현재 사이드바에서 선택(클릭)된 폴더 — 서브그룹 추가·이름변경에 사용.
    var selectedFolderItem = null;
    var renamingFolderId   = null;
    var pendingConfirm     = null;

    // ── 사이드바 폴더 클릭: 선택 추적 ─────────────────────────
    // 링크 자체는 href 로 /favorites?folder_id=X 로 이동한다.
    // CRUD 버튼 / caret 토글 클릭은 여기서 잡지 않도록 closest() 로 걸러낸다.
    document.querySelectorAll('.fav-sidebar-item').forEach(function (li) {
        li.addEventListener('click', function (e) {
            if (e.target.closest('.fav-sidebar-item__btn')) { return; }
            if (e.target.closest('.fav-sidebar-item__caret')) { return; }
            selectedFolderItem = li;
            var depth = parseInt(li.dataset.folderDepth, 10);
            addSubBtn.disabled = (depth !== 0);
            // active 클래스 갱신 (SSR 에서 이미 active 인 경우와의 일관성 유지)
            document.querySelectorAll('.fav-sidebar-item--active').forEach(function (el) {
                el.classList.remove('fav-sidebar-item--active');
            });
            li.classList.add('fav-sidebar-item--active');
        });
    });

    // ── task 00037 #1: 루트 그룹 caret 토글 (접기/펼치기) ────
    // 기본 상태는 펼침(HTML 에서 aria-expanded=\"true\", 자식 <ul> 에 is-collapsed
    // 클래스가 없음). caret 클릭 시 자식 <ul> 의 is-collapsed 토글 + aria-expanded
    // 값 반전 + 화살표 텍스트 교체(▾↔▸) 를 함께 수행한다.
    document.addEventListener('click', function (e) {
        var caret = e.target.closest('.fav-sidebar-item__caret');
        if (!caret) { return; }
        e.stopPropagation();
        var targetId = caret.dataset.targetId;
        if (!targetId) { return; }
        var childList = document.getElementById(targetId);
        if (!childList) { return; }
        var expanded = caret.getAttribute('aria-expanded') === 'true';
        if (expanded) {
            childList.classList.add('is-collapsed');
            caret.setAttribute('aria-expanded', 'false');
            caret.textContent = '▸';
        } else {
            childList.classList.remove('is-collapsed');
            caret.setAttribute('aria-expanded', 'true');
            caret.textContent = '▾';
        }
    });

    // ── 그룹(루트) 추가 ──────────────────────────────────────
    addRootBtn.addEventListener('click', function () {
        createFolder(null);
    });

    // ── 서브그룹 추가 ────────────────────────────────────────
    addSubBtn.addEventListener('click', function () {
        if (!selectedFolderItem || parseInt(selectedFolderItem.dataset.folderDepth, 10) !== 0) {
            return;
        }
        createFolder(selectedFolderItem.dataset.folderId);
    });

    function createFolder(parentId) {
        var name = newNameInput.value.trim();
        if (!name) {
            sidebarError.textContent = '폴더 이름을 입력하세요.';
            return;
        }
        sidebarError.textContent = '';
        fetch('/favorites/folders', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: name, parent_id: parentId ? parseInt(parentId, 10) : null }),
        })
        .then(function (r) {
            if (!r.ok) {
                return r.json().catch(function () { return {}; }).then(function (d) {
                    throw new Error(d.detail || '폴더 생성 실패');
                });
            }
            return r.json();
        })
        .then(function () {
            window.location.reload();
        })
        .catch(function (err) {
            sidebarError.textContent = err.message || '폴더 생성 실패.';
        });
    }

    // ── 이름 변경 버튼 ───────────────────────────────────────
    document.addEventListener('click', function (e) {
        var btn = e.target.closest('.fav-sidebar-item__rename-btn');
        if (!btn) { return; }
        e.stopPropagation();
        renamingFolderId = btn.dataset.folderId;
        renameInput.value = btn.dataset.folderName || '';
        renameError.textContent = '';
        renameDialog.showModal();
    });

    renameSaveBtn.addEventListener('click', function () {
        var name = renameInput.value.trim();
        if (!name) {
            renameError.textContent = '이름을 입력하세요.';
            return;
        }
        renameError.textContent = '';
        fetch('/favorites/folders/' + renamingFolderId, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: name }),
        })
        .then(function (r) {
            if (!r.ok) {
                return r.json().catch(function () { return {}; }).then(function (d) {
                    throw new Error(d.detail || '변경 실패');
                });
            }
            return r.json();
        })
        .then(function () {
            renameDialog.close();
            window.location.reload();
        })
        .catch(function (err) {
            renameError.textContent = err.message || '변경 실패.';
        });
    });

    renameCancelBtn.addEventListener('click', function () { renameDialog.close(); });
    renameDialog.addEventListener('click', function (e) {
        if (e.target === renameDialog) { renameDialog.close(); }
    });

    // ── 폴더 삭제 버튼 ───────────────────────────────────────
    // task 00037 #2: 삭제 확정 전에 GET /favorites/folders/{id}/delete-preview 로
    // 하위 서브그룹 / 공고 개수를 받아 사용자에게 cascade 경고를 명확히 보여준다.
    // preview 가 실패하면 개수 정보 없이 기본 문구로 fallback (네트워크 경계 안전).
    document.addEventListener('click', function (e) {
        var btn = e.target.closest('.fav-sidebar-item__delete-btn');
        if (!btn) { return; }
        e.stopPropagation();
        var folderId = btn.dataset.folderId;
        var folderName = btn.dataset.folderName || '폴더';

        // 먼저 preview 호출 — 성공 시 정확한 개수로, 실패 시 fallback 메시지로 확인.
        fetch('/favorites/folders/' + folderId + '/delete-preview')
            .then(function (r) {
                if (!r.ok) { throw new Error('preview 실패'); }
                return r.json();
            })
            .then(function (preview) {
                var message = buildDeleteMessage(folderName, preview);
                confirmDeleteFolder(folderId, message);
            })
            .catch(function () {
                // preview 실패 — 개수 없이도 삭제 확인은 받는다.
                confirmDeleteFolder(
                    folderId,
                    '\"' + folderName + '\" 폴더와 폴더 안의 모든 즐겨찾기를 삭제합니다. 계속하시겠습니까?'
                );
            });
    });

    function buildDeleteMessage(folderName, preview) {
        /**
         * preview 응답 ({subfolder_count, entry_count}) 을 받아 사용자에게 표시할
         * cascade 경고 문구를 만든다. 둘 다 0 이면 단순 삭제 문구, 하나 이상이면
         * \"하위 서브그룹 N개, 공고 M건이 함께 삭제됩니다\" 경고를 붙인다.
         */
        var sub = preview.subfolder_count || 0;
        var ent = preview.entry_count || 0;
        var header = '\"' + folderName + '\" 폴더를 삭제합니다.';
        if (sub === 0 && ent === 0) {
            return header + '\n\n폴더 안에 항목이 없습니다.';
        }
        var parts = [];
        if (sub > 0) { parts.push('하위 서브그룹 ' + sub + '개'); }
        if (ent > 0) { parts.push('공고 ' + ent + '건'); }
        return header + '\n\n' + parts.join(', ') + '이 함께 삭제됩니다.\n이 작업은 되돌릴 수 없습니다.';
    }

    function confirmDeleteFolder(folderId, message) {
        /**
         * confirm dialog 를 띄우고 OK 시 DELETE /favorites/folders/{id} 를 실행한다.
         * 성공/실패 모두 페이지를 reload 해 좌측 트리 상태를 서버와 동기화한다.
         */
        showConfirm(
            '폴더 삭제',
            message,
            function () {
                fetch('/favorites/folders/' + folderId, { method: 'DELETE' })
                .then(function (r) {
                    if (!r.ok) { throw new Error('삭제 실패'); }
                    window.location.reload();
                })
                .catch(function () {
                    window.location.reload();
                });
            }
        );
    }

    // ── 즐겨찾기 항목 제거 버튼 ─────────────────────────────
    document.addEventListener('click', function (e) {
        var btn = e.target.closest('.fav-entry-remove-btn');
        if (!btn) { return; }
        e.stopPropagation();
        var entryId = btn.dataset.entryId;
        showConfirm(
            '즐겨찾기 제거',
            '이 과제를 즐겨찾기에서 제거합니다.',
            function () {
                fetch('/favorites/entries/' + entryId, { method: 'DELETE' })
                .then(function (r) {
                    if (!r.ok && r.status !== 404) { throw new Error('삭제 실패'); }
                    var row = btn.closest('tr');
                    if (row) { row.remove(); }
                })
                .catch(function () {
                    var row = btn.closest('tr');
                    if (row) { row.remove(); }
                });
            }
        );
    });

    // ── confirm dialog helper ────────────────────────────────
    function showConfirm(title, message, callback) {
        confirmTitle.textContent   = title;
        confirmMessage.textContent = message;
        pendingConfirm = callback;
        confirmDialog.showModal();
    }

    confirmOkBtn.addEventListener('click', function () {
        confirmDialog.close();
        if (pendingConfirm) {
            pendingConfirm();
            pendingConfirm = null;
        }
    });

    confirmCancelBtn.addEventListener('click', function () {
        confirmDialog.close();
        pendingConfirm = null;
    });

    confirmDialog.addEventListener('click', function (e) {
        if (e.target === confirmDialog) {
            confirmDialog.close();
            pendingConfirm = null;
        }
    });
}());
