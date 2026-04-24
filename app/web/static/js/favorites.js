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
        /**
         * 서버에서 받은 폴더 트리(루트 + depth 1) 를 indent 트리로 렌더한다.
         * task 00037 #1: 사이드바와 동일한 시각 체계 — caret 토글은 자식을 가진
         * 루트에만 붙이고, 자식 <ul> 은 루트 li 의 block 형제로 붙여 indent 를
         * 준다. 폴더 선택(클릭) 은 .fav-folder-item__row 에서만 받고, caret 클릭은
         * stopPropagation 으로 선택과 분리한다. 기본 펼침 상태.
         */
        folderTreeEl.innerHTML = '';
        if (!folders.length) {
            folderTreeEl.innerHTML = '<p class="fav-folder-tree__empty">폴더가 없습니다. 새 그룹을 만들어 추가하세요.</p>';
            return;
        }
        var ul = document.createElement('ul');
        ul.className = 'fav-folder-list';
        folders.forEach(function (folder) {
            var hasChildren = !!(folder.children && folder.children.length);
            var li = makeFolderItem(folder, hasChildren);
            ul.appendChild(li);
            if (hasChildren) {
                var childUl = document.createElement('ul');
                childUl.className = 'fav-folder-list fav-folder-list--child';
                childUl.dataset.childListForRoot = String(folder.id);
                folder.children.forEach(function (child) {
                    childUl.appendChild(makeFolderItem(child, false));
                });
                li.appendChild(childUl);
            }
        });
        folderTreeEl.appendChild(ul);
    }

    function makeFolderItem(folder, isRootWithChildren) {
        /**
         * 개별 폴더 항목 li 를 생성한다.
         * - row(flex): [caret|spacer] + 레이블
         * - row 클릭 → 폴더 선택 (저장 대상 지정)
         * - caret 클릭 → 하위 ul 접기/펼치기 (stopPropagation 으로 선택과 분리)
         * depth=0 이고 자식이 있으면 caret, 그 외에는 spacer 로 폭만 맞춘다.
         */
        var li = document.createElement('li');
        li.className = 'fav-folder-item';
        li.dataset.folderId = folder.id;
        li.dataset.folderDepth = folder.depth;

        var row = document.createElement('div');
        row.className = 'fav-folder-item__row';

        if (isRootWithChildren) {
            var caret = document.createElement('button');
            caret.type = 'button';
            caret.className = 'fav-folder-item__caret';
            caret.setAttribute('aria-expanded', 'true');
            caret.title = '서브그룹 접기/펼치기';
            caret.textContent = '▾';
            caret.addEventListener('click', function (e) {
                e.stopPropagation();
                toggleModalChildList(li, caret);
            });
            row.appendChild(caret);
        } else {
            var spacer = document.createElement('span');
            spacer.className = 'fav-folder-item__caret-spacer';
            spacer.setAttribute('aria-hidden', 'true');
            row.appendChild(spacer);
        }

        var label = document.createElement('span');
        label.className = 'fav-folder-item__label';
        label.textContent = folder.name;
        row.appendChild(label);

        row.addEventListener('click', function (e) {
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

        li.appendChild(row);
        return li;
    }

    function toggleModalChildList(rootLi, caret) {
        /**
         * 모달 트리에서 루트 폴더의 자식 리스트를 접기/펼치기 한다.
         * rootLi 의 직속 자식 중 .fav-folder-list--child 가 있으면 is-collapsed
         * 클래스를 토글하고 caret 의 aria-expanded 값과 화살표 텍스트를 맞춘다.
         */
        var childUl = rootLi.querySelector(':scope > .fav-folder-list--child');
        if (!childUl) { return; }
        var expanded = caret.getAttribute('aria-expanded') === 'true';
        if (expanded) {
            childUl.classList.add('is-collapsed');
            caret.setAttribute('aria-expanded', 'false');
            caret.textContent = '▸';
        } else {
            childUl.classList.remove('is-collapsed');
            caret.setAttribute('aria-expanded', 'true');
            caret.textContent = '▾';
        }
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
