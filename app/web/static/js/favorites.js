// 즐겨찾기 별 아이콘 + 폴더 선택 모달 (Phase 3b / 00036-6)
// Jinja2 템플릿 변수 없이 순수 JS 로만 동작한다.
// 모달이 없는 페이지(비로그인, 관리자 페이지 등)에서는 초기화 직후 반환한다.
(function () {
    'use strict';

    var modal = document.getElementById('fav-modal');
    if (!modal) { return; }

    var folderTreeEl = document.getElementById('fav-folder-tree');
    var newFolderNameEl = document.getElementById('fav-new-folder-name');
    var addRootBtn = document.getElementById('fav-add-root-btn');
    var addSubBtn = document.getElementById('fav-add-sub-btn');
    var saveBtn = document.getElementById('fav-modal-save-btn');
    var cancelBtn = document.getElementById('fav-modal-cancel-btn');
    var errorMsg = document.getElementById('fav-modal-error');

    // 현재 모달 대상 canonical_project_id.
    var currentCanonicalId = null;
    // 현재 선택된 폴더 {id, depth}.
    var selectedFolder = null;

    // ── 별 클릭 이벤트 위임 ──────────────────────────────────────
    document.addEventListener('click', function (e) {
        var btn = e.target.closest('.fav-star');
        if (!btn) { return; }
        e.stopPropagation();

        var canonicalId = btn.dataset.canonicalId;
        var entryId = btn.dataset.entryId;

        if (!canonicalId) { return; }

        if (entryId) {
            // 이미 즐겨찾기됨 → 즉시 제거
            removeFavorite(canonicalId, entryId);
        } else {
            // 미즐겨찾기 → 폴더 선택 모달 열기
            openModal(canonicalId);
        }
    });

    // ── 모달 열기 ────────────────────────────────────────────────
    function openModal(canonicalId) {
        currentCanonicalId = canonicalId;
        selectedFolder = null;
        saveBtn.disabled = true;
        addSubBtn.disabled = true;
        errorMsg.textContent = '';
        newFolderNameEl.value = '';
        folderTreeEl.innerHTML = '<p class="fav-folder-tree__loading">로딩 중…</p>';
        modal.showModal();
        loadFolderTree();
    }

    // ── 폴더 목록 조회 + 렌더 ────────────────────────────────────
    function loadFolderTree() {
        fetch('/favorites/folders')
            .then(function (r) { return r.json(); })
            .then(function (data) { renderFolderTree(data.folders || []); })
            .catch(function () {
                folderTreeEl.innerHTML = '<p class="fav-folder-tree__error">폴더를 불러오지 못했습니다.</p>';
            });
    }

    function renderFolderTree(folders) {
        folderTreeEl.innerHTML = '';
        if (!folders.length) {
            folderTreeEl.innerHTML = '<p class="fav-folder-tree__empty">폴더가 없습니다. 새 그룹을 만들어 추가하세요.</p>';
            return;
        }
        var ul = document.createElement('ul');
        ul.className = 'fav-folder-list';
        folders.forEach(function (folder) {
            var li = makeFolderItem(folder);
            ul.appendChild(li);
            if (folder.children && folder.children.length) {
                var childUl = document.createElement('ul');
                childUl.className = 'fav-folder-list fav-folder-list--child';
                folder.children.forEach(function (child) {
                    childUl.appendChild(makeFolderItem(child));
                });
                li.appendChild(childUl);
            }
        });
        folderTreeEl.appendChild(ul);
    }

    function makeFolderItem(folder) {
        var li = document.createElement('li');
        li.className = 'fav-folder-item';
        li.dataset.folderId = folder.id;
        li.dataset.folderDepth = folder.depth;
        li.textContent = folder.name;
        li.addEventListener('click', function (e) {
            e.stopPropagation();
            folderTreeEl.querySelectorAll('.fav-folder-item--selected').forEach(function (el) {
                el.classList.remove('fav-folder-item--selected');
            });
            li.classList.add('fav-folder-item--selected');
            selectedFolder = { id: folder.id, depth: folder.depth };
            saveBtn.disabled = false;
            // depth 0(root) 선택 시에만 서브그룹 추가 활성
            addSubBtn.disabled = (folder.depth !== 0);
        });
        return li;
    }

    // ── 그룹 추가 버튼 (루트 레벨) ──────────────────────────────
    addRootBtn.addEventListener('click', function () {
        createFolder(null);
    });

    // ── 서브그룹 추가 버튼 (선택된 루트 하위) ───────────────────
    addSubBtn.addEventListener('click', function () {
        if (!selectedFolder || selectedFolder.depth !== 0) { return; }
        createFolder(selectedFolder.id);
    });

    function createFolder(parentId) {
        var name = newFolderNameEl.value.trim();
        if (!name) {
            errorMsg.textContent = '폴더 이름을 입력하세요.';
            return;
        }
        errorMsg.textContent = '';
        fetch('/favorites/folders', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: name, parent_id: parentId }),
        })
        .then(function (r) {
            if (!r.ok) {
                return r.json().catch(function () { return {}; }).then(function (d) {
                    throw new Error(d.detail || '폴더 생성 실패');
                });
            }
            return r.json();
        })
        .then(function (newFolder) {
            newFolderNameEl.value = '';
            // 트리 다시 로드 후 새 폴더 자동 선택
            fetch('/favorites/folders')
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    renderFolderTree(data.folders || []);
                    var newItem = folderTreeEl.querySelector('[data-folder-id="' + newFolder.id + '"]');
                    if (newItem) { newItem.click(); }
                });
        })
        .catch(function (err) {
            errorMsg.textContent = err.message || '폴더 생성 실패.';
        });
    }

    // ── 추가(저장) 버튼 ──────────────────────────────────────────
    saveBtn.addEventListener('click', function () {
        if (!selectedFolder || !currentCanonicalId) { return; }
        errorMsg.textContent = '';
        fetch('/favorites/entries', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                folder_id: parseInt(selectedFolder.id, 10),
                canonical_project_id: parseInt(currentCanonicalId, 10),
            }),
        })
        .then(function (r) {
            if (!r.ok) {
                return r.json().catch(function () { return {}; }).then(function (d) {
                    throw new Error(d.detail || '추가 실패');
                });
            }
            return r.json();
        })
        .then(function (entry) {
            updateAllStars(currentCanonicalId, entry.id);
            modal.close();
        })
        .catch(function (err) {
            errorMsg.textContent = err.message || '저장 실패.';
        });
    });

    // ── 즐겨찾기 제거 ────────────────────────────────────────────
    function removeFavorite(canonicalId, entryId) {
        fetch('/favorites/entries/' + entryId, { method: 'DELETE' })
        .then(function (r) {
            if (!r.ok) { throw new Error('삭제 실패'); }
            updateAllStars(canonicalId, '');
        })
        .catch(function () {
            // 이미 삭제됐거나 네트워크 오류 — 페이지 상태를 건드리지 않는다
        });
    }

    // ── 같은 canonical 의 모든 별 아이콘 동기화 ─────────────────
    function updateAllStars(canonicalId, entryId) {
        document.querySelectorAll('.fav-star[data-canonical-id="' + canonicalId + '"]').forEach(function (btn) {
            if (entryId) {
                btn.dataset.entryId = String(entryId);
                btn.classList.add('fav-star--active');
                btn.setAttribute('title', '즐겨찾기 제거');
                btn.textContent = '★'; // ★
            } else {
                btn.dataset.entryId = '';
                btn.classList.remove('fav-star--active');
                btn.setAttribute('title', '즐겨찾기 추가');
                btn.textContent = '☆'; // ☆
            }
        });
    }

    // ── 닫기 버튼 + 배경 클릭 ───────────────────────────────────
    cancelBtn.addEventListener('click', function () { modal.close(); });
    modal.addEventListener('click', function (e) {
        if (e.target === modal) { modal.close(); }
    });
}());
