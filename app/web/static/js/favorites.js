// 즐겨찾기 별 아이콘 + 폴더 선택 모달 (task 00037 announcement 단위 재배선).
// Jinja2 템플릿 변수 없이 순수 JS 로만 동작한다.
// 모달이 없는 페이지(비로그인, 관리자 페이지 등)에서는 초기화 직후 반환한다.
//
// 지원하는 모달 모드(#fav-modal[data-mode]):
//   - \"add\" (기본): 별 아이콘에서 열린다. 라디오(이 공고만/동일 과제 모두) +
//                    폴더 트리 + 새 폴더 만들기 UI 를 모두 사용. 저장은 POST
//                    /favorites/entries.
//   - \"move\":       /favorites 의 폴더 이동 버튼에서 열린다. 라디오/새 폴더
//                    영역은 CSS 로 숨기고, 트리에서 대상 폴더 선택 → 저장은
//                    PATCH /favorites/entries/{entry_id}.
(function () {
    'use strict';

    var modal = document.getElementById('fav-modal');
    if (!modal) { return; }

    var titleEl = document.getElementById('fav-modal-title');
    var helpEl = document.getElementById('fav-modal-help');
    var folderTreeEl = document.getElementById('fav-folder-tree');
    var newFolderNameEl = document.getElementById('fav-new-folder-name');
    var addRootBtn = document.getElementById('fav-add-root-btn');
    var addSubBtn = document.getElementById('fav-add-sub-btn');
    var saveBtn = document.getElementById('fav-modal-save-btn');
    var cancelBtn = document.getElementById('fav-modal-cancel-btn');
    var errorMsg = document.getElementById('fav-modal-error');
    var scopeGroup = document.getElementById('fav-scope-group');

    // 모드별 상태 변수
    //   add 모드: currentAnnouncementId / currentCanonicalId (dataset 에서 읽음)
    //   move 모드: currentMovingEntryId / currentMovingFromFolderId
    var currentAnnouncementId = null;
    var currentCanonicalId = null;
    var currentMovingEntryId = null;
    var currentMovingFromFolderId = null;
    // 현재 선택된 폴더 {id, depth}. 모드와 무관하게 트리 클릭이 세팅.
    var selectedFolder = null;
    // 외부(favorites_page.js) 에서 호출 가능한 public helper 를 노출하기 위해
    // window.favoritesModal 네임스페이스에 오픈 함수를 붙인다 — 별표 클릭은
    // 내부 리스너로 처리하고, 폴더 이동은 외부 JS 가 openMoveMode 를 호출한다.
    window.favoritesModal = window.favoritesModal || {};
    window.favoritesModal.openMoveMode = openMoveMode;

    // ── 별 클릭 이벤트 위임 ──────────────────────────────────────
    // task 00037 #4: announcement 단위 전환 — data-announcement-id 를 1차 키로
    // 사용한다. 이미 즐겨찾기된 공고는 즉시 제거(entry_id 존재), 아니면 모달을
    // 연다. data-canonical-id 는 \"동일 과제 모두 저장\" 라디오 플로우에서 사용.
    document.addEventListener('click', function (e) {
        var btn = e.target.closest('.fav-star');
        if (!btn) { return; }
        e.stopPropagation();

        var announcementId = btn.dataset.announcementId;
        var canonicalId = btn.dataset.canonicalId || '';
        var entryId = btn.dataset.entryId;

        if (!announcementId) { return; }

        if (entryId) {
            // 이미 즐겨찾기됨 → 즉시 제거 (현재 공고 1건만 unstar)
            removeFavorite(announcementId, entryId);
        } else {
            // 미즐겨찾기 → 폴더 선택 모달 열기
            openAddMode(announcementId, canonicalId);
        }
    });

    // ── 모달 열기 (추가 모드) ────────────────────────────────────
    function openAddMode(announcementId, canonicalId) {
        /**
         * 별 아이콘 클릭에서 호출. 추가 모드로 모달을 초기화한다.
         * - 라디오 상태는 항상 \"single(이 공고만)\" 으로 초기화(guidance 명시).
         * - 동일 canonical 공고가 여러 개가 아니라면 \"동일 과제 모두\" 라디오는
         *   의미 없으므로 비활성화한다(canonical id 가 비어 있을 때).
         */
        modal.dataset.mode = 'add';
        currentAnnouncementId = announcementId;
        currentCanonicalId = canonicalId || '';
        currentMovingEntryId = null;
        currentMovingFromFolderId = null;
        selectedFolder = null;
        saveBtn.disabled = true;
        saveBtn.textContent = '추가';
        addSubBtn.disabled = true;
        errorMsg.textContent = '';
        newFolderNameEl.value = '';
        titleEl.textContent = '폴더에 추가';
        helpEl.textContent = '저장할 폴더를 선택하세요.';
        resetScopeRadio();
        setScopeDisabled(!currentCanonicalId);
        folderTreeEl.innerHTML = '<p class="fav-folder-tree__loading">로딩 중…</p>';
        modal.showModal();
        loadFolderTree();
    }

    // ── 모달 열기 (이동 모드) ────────────────────────────────────
    function openMoveMode(entryId, fromFolderId) {
        /**
         * /favorites 의 \"폴더 이동\" 버튼에서 호출. 이동 모드로 모달을 초기화한다.
         * 라디오/새 폴더 영역은 CSS(#fav-modal[data-mode=\"move\"]) 로 자동 hide.
         */
        modal.dataset.mode = 'move';
        currentAnnouncementId = null;
        currentCanonicalId = null;
        currentMovingEntryId = String(entryId);
        currentMovingFromFolderId = fromFolderId !== undefined && fromFolderId !== null
            ? String(fromFolderId)
            : null;
        selectedFolder = null;
        saveBtn.disabled = true;
        saveBtn.textContent = '이동';
        addSubBtn.disabled = true;
        errorMsg.textContent = '';
        newFolderNameEl.value = '';
        titleEl.textContent = '폴더 이동';
        helpEl.textContent = '이동할 폴더를 선택하세요.';
        resetScopeRadio();
        folderTreeEl.innerHTML = '<p class="fav-folder-tree__loading">로딩 중…</p>';
        modal.showModal();
        loadFolderTree();
    }

    function resetScopeRadio() {
        /**
         * 라디오 상태를 \"single(이 공고만)\" 으로 초기화한다. guidance 지시:
         * \"모달 라디오 상태는 모달 열릴 때마다 '이 공고만' 으로 초기화\".
         */
        if (!scopeGroup) { return; }
        var radios = scopeGroup.querySelectorAll('input[type="radio"][name="fav-scope"]');
        radios.forEach(function (r) { r.checked = (r.value === 'single'); });
    }

    function setScopeDisabled(disabled) {
        /**
         * \"동일 과제 모두 저장\" 라디오를 canonical 없음(매칭 전 공고) 경우 비활성화
         * 한다. \"이 공고만 저장\" 은 항상 선택 가능.
         */
        if (!scopeGroup) { return; }
        var allRadio = scopeGroup.querySelector('input[type="radio"][value="all_siblings"]');
        if (allRadio) {
            allRadio.disabled = !!disabled;
            if (disabled && allRadio.checked) {
                // 비활성화와 동시에 \"이 공고만\" 으로 되돌린다.
                var singleRadio = scopeGroup.querySelector('input[type="radio"][value="single"]');
                if (singleRadio) { singleRadio.checked = true; }
            }
        }
    }

    function getSelectedScope() {
        /**
         * 현재 체크된 라디오 값을 반환. 기본 \"single\". \"all_siblings\" 도 가능.
         */
        if (!scopeGroup) { return 'single'; }
        var checked = scopeGroup.querySelector('input[type="radio"][name="fav-scope"]:checked');
        return checked ? checked.value : 'single';
    }

    // 라디오 자체 클릭 시 모달 배경 클릭으로 닫히지 않도록 stopPropagation.
    // (dialog 기본 동작 외 사용자 핸들러가 배경/외부 클릭으로 취급하지 않게 방어)
    if (scopeGroup) {
        scopeGroup.addEventListener('click', function (e) { e.stopPropagation(); });
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
            // 서브그룹 추가 버튼: add 모드 + depth 0 선택일 때만 활성.
            addSubBtn.disabled = (modal.dataset.mode !== 'add' || folder.depth !== 0);
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
        if (modal.dataset.mode !== 'add') { return; }
        createFolder(null);
    });

    // ── 서브그룹 추가 버튼 (선택된 루트 하위) ───────────────────
    addSubBtn.addEventListener('click', function () {
        if (modal.dataset.mode !== 'add') { return; }
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
                    if (newItem) {
                        // row 를 클릭한 효과를 내기 위해 직접 row 요소를 클릭.
                        var row = newItem.querySelector(':scope > .fav-folder-item__row');
                        if (row) { row.click(); }
                    }
                });
        })
        .catch(function (err) {
            errorMsg.textContent = err.message || '폴더 생성 실패.';
        });
    }

    // ── 저장(추가 또는 이동) 버튼 ────────────────────────────────
    saveBtn.addEventListener('click', function () {
        if (!selectedFolder) { return; }
        errorMsg.textContent = '';
        if (modal.dataset.mode === 'move') {
            performMove();
        } else {
            performAdd();
        }
    });

    function performAdd() {
        /**
         * 추가 모드 저장 — POST /favorites/entries.
         * 라디오 값에 따라 apply_to_all_siblings 를 True/False 로 전송.
         * 응답의 created_entries 를 announcement_id → entry_id 맵으로 풀어서
         * updateAllStars 로 각 별 아이콘을 개별 동기화한다.
         * skipped_announcement_ids (이미 해당 폴더에 있음) 는 entry_id 가 없어
         * DB 에 존재하긴 하지만 본 모달 호출에서 얻은 entry_id 는 없다. DOM 상
         * 이미 채워진 별(★) 이라면 그대로 두고, 비어 있는 별이라면 반응하지
         * 않는다(정확한 entry_id 를 모르는 채 표시만 바꾸면 제거 클릭이 깨진다).
         */
        if (!currentAnnouncementId) { return; }
        var scope = getSelectedScope();
        var applyAll = (scope === 'all_siblings');
        fetch('/favorites/entries', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                folder_id: parseInt(selectedFolder.id, 10),
                announcement_id: parseInt(currentAnnouncementId, 10),
                apply_to_all_siblings: applyAll,
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
        .then(function (payload) {
            var createdEntries = payload.created_entries || [];
            createdEntries.forEach(function (entry) {
                updateStarsForAnnouncement(String(entry.announcement_id), String(entry.id));
            });
            // skipped 에는 entry_id 가 포함되지 않는다 — DOM 이 이미 ★ 이면 그대로.
            // 그러나 현재 클릭한 announcement 가 skipped 에 있다면 그 별도 이미
            // 채워진 상태로 남아 있어야 하므로 별도 처리 불필요.
            modal.close();
        })
        .catch(function (err) {
            errorMsg.textContent = err.message || '저장 실패.';
        });
    }

    function performMove() {
        /**
         * 이동 모드 저장 — PATCH /favorites/entries/{entry_id}.
         * 성공 시 페이지를 reload 해 서버 상태와 동기화(간단하고 확실).
         * 이동 대상 폴더와 현재 폴더가 같다면 서버가 moved=false 를 반환한다.
         */
        if (!currentMovingEntryId) { return; }
        if (currentMovingFromFolderId !== null &&
            String(selectedFolder.id) === currentMovingFromFolderId) {
            // 동일 폴더 이동은 no-op — 모달만 닫는다(네트워크 왕복 생략).
            modal.close();
            return;
        }
        fetch('/favorites/entries/' + currentMovingEntryId, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ target_folder_id: parseInt(selectedFolder.id, 10) }),
        })
        .then(function (r) {
            if (!r.ok) {
                return r.json().catch(function () { return {}; }).then(function (d) {
                    throw new Error(d.detail || '이동 실패');
                });
            }
            return r.json();
        })
        .then(function () {
            modal.close();
            window.location.reload();
        })
        .catch(function (err) {
            errorMsg.textContent = err.message || '이동 실패.';
        });
    }

    // ── 즐겨찾기 제거 (별 하나 unstar) ──────────────────────────
    function removeFavorite(announcementId, entryId) {
        fetch('/favorites/entries/' + entryId, { method: 'DELETE' })
        .then(function (r) {
            if (!r.ok && r.status !== 404) { throw new Error('삭제 실패'); }
            updateStarsForAnnouncement(announcementId, '');
        })
        .catch(function () {
            // 이미 삭제됐거나 네트워크 오류 — 페이지 상태를 건드리지 않는다
        });
    }

    // ── 별 아이콘 동기화 (announcement_id 단위) ─────────────────
    function updateStarsForAnnouncement(announcementId, entryId) {
        /**
         * 동일 announcement_id 를 가진 모든 별 아이콘의 표시/데이터를 맞춘다.
         * task 00037 #4: 단위가 canonical → announcement 로 바뀌었기 때문에
         * 매칭 셀렉터도 data-announcement-id 로 전환. bulk 등록 결과의 각 entry
         * 를 이 함수에 순서대로 먹여 announcement 별로 정확히 업데이트한다.
         */
        document.querySelectorAll(
            '.fav-star[data-announcement-id="' + announcementId + '"]'
        ).forEach(function (btn) {
            if (entryId) {
                btn.dataset.entryId = String(entryId);
                btn.classList.add('fav-star--active');
                btn.setAttribute('title', '즐겨찾기 제거');
                btn.textContent = '★';
            } else {
                btn.dataset.entryId = '';
                btn.classList.remove('fav-star--active');
                btn.setAttribute('title', '즐겨찾기 추가');
                btn.textContent = '☆';
            }
        });
    }

    // ── 닫기 버튼 + 배경 클릭 ───────────────────────────────────
    cancelBtn.addEventListener('click', function () { modal.close(); });
    modal.addEventListener('click', function (e) {
        if (e.target === modal) { modal.close(); }
    });
}());
