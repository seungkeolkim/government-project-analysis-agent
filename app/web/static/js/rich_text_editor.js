/*
  게시글 리치 텍스트 에디터 (task 00153-2).

  === 에디터 선택 근거 ===
  - 프로젝트 하드 룰: 외부 CDN 금지(PROJECT_NOTES: "static/ 로컬 정적 리소스,
    외부 CDN 금지") + 무빌드(npm 빌드 단계 없이 정적 자산 드롭-인).
  - 추가 제약(본 작업 환경): 패키지 매니저 실행 금지 → TinyMCE/CKEditor 같은
    서드파티 배포본을 네트워크에서 받아 static/ 에 넣는 것이 불가능하다.
  - 따라서 외부 의존성 0 으로 동작하는 자체 구현 에디터를 채택했다. 브라우저
    기본 기능인 contenteditable + document.execCommand 로 표·폰트·서식·목록·
    들여쓰기를 제공하고, Word/Outlook 클립보드 붙여넣기는 contenteditable 의
    기본 붙여넣기(클립보드 HTML 보존) 동작을 그대로 활용한다. execCommand 는
    표준에서 deprecated 로 분류되지만 모든 주요 브라우저(Chromium 포함)가 여전히
    지원하며, 무의존성 리치 텍스트 편집기의 사실상 표준 구현 방식이다.

  === 보안 ===
  - 클라이언트가 만들거나 붙여넣은 HTML 은 신뢰하지 않는다. 실제 정화(XSS 제거)
    는 서버측 sanitization(00153-1, app/suggestions/sanitize.py)이 저장 시점에
    책임진다. 본 스크립트는 편의(입력 UI)만 담당한다.
  - 단, 클라이언트 허용 서식과 서버 allowlist 가 어긋나 서식이 통째로 사라지지
    않도록, 폰트(font/색상/크기)·표·목록·정렬 등 서버가 보존하는 요소만 생성한다.

  === 폼 연동 ===
  - 대상은 ``textarea[data-rich-editor]``. 해당 textarea 를 숨기고 그 자리에
    툴바 + contenteditable 편집 영역을 만든다.
  - 같은 form 안의 ``input[name=body_format]`` 값을 'html' 로 강제한다(00153-1
    의 라우트 계약과 일치).
  - 폼 제출 직전 편집 영역의 innerHTML 을 숨겨진 textarea(name=body)에 동기화한다.
  - 수정 화면: textarea 에 들어 있는 기존 본문을 초기값으로 로드한다. 초기 포맷
    (data-initial-format)이 'html' 이면 innerHTML, 'plain' 이면 줄바꿈 보존
    텍스트로 로드한다(하위 호환).
*/

(function () {
    "use strict";

    /**
     * 텍스트를 HTML 로 안전하게 이스케이프한다.
     *
     * 평문 본문(하위 호환)을 편집 영역에 로드할 때, 본문에 우연히 들어 있는
     * '<' '>' '&' 같은 문자가 태그로 해석되지 않도록 엔티티로 변환한다.
     *
     * @param {string} text 원본 평문.
     * @returns {string} 이스케이프된 HTML.
     */
    function escapeHtml(text) {
        const div = document.createElement("div");
        div.textContent = text;
        return div.innerHTML;
    }

    /**
     * 평문(줄바꿈 포함)을 편집 영역용 HTML 로 변환한다.
     *
     * 평문 게시글을 리치 에디터로 열 때 줄바꿈이 사라지지 않도록 '\n' 을
     * <br> 로 치환한다.
     *
     * @param {string} text 원본 평문.
     * @returns {string} 줄바꿈이 <br> 로 보존된 HTML.
     */
    function plainTextToHtml(text) {
        return escapeHtml(text).replace(/\r\n|\r|\n/g, "<br>");
    }

    /**
     * 툴바 버튼 1개를 생성한다.
     *
     * @param {string} label 버튼에 표시할 텍스트/기호.
     * @param {string} title 마우스 오버 툴팁(접근성 라벨 겸용).
     * @param {function(MouseEvent): void} onClick 클릭 핸들러.
     * @returns {HTMLButtonElement} 생성된 버튼.
     */
    function createToolbarButton(label, title, onClick) {
        const button = document.createElement("button");
        button.type = "button"; // form submit 방지 — 반드시 button 타입.
        button.className = "rich-text-editor__btn";
        button.textContent = label;
        button.title = title;
        button.setAttribute("aria-label", title);
        button.addEventListener("click", function (event) {
            event.preventDefault();
            onClick(event);
        });
        return button;
    }

    /**
     * select 드롭다운(폰트 종류·크기 등)을 생성한다.
     *
     * @param {string} title 접근성 라벨.
     * @param {Array<{value: string, label: string}>} options 옵션 목록(첫 항목은 placeholder).
     * @param {function(string): void} onChange 값 선택 시 콜백.
     * @returns {HTMLSelectElement} 생성된 select.
     */
    function createToolbarSelect(title, options, onChange) {
        const select = document.createElement("select");
        select.className = "rich-text-editor__select";
        select.title = title;
        select.setAttribute("aria-label", title);
        options.forEach(function (option) {
            const optionElement = document.createElement("option");
            optionElement.value = option.value;
            optionElement.textContent = option.label;
            select.appendChild(optionElement);
        });
        select.addEventListener("change", function () {
            const selectedValue = select.value;
            if (selectedValue) {
                onChange(selectedValue);
            }
            // 선택 후 placeholder 로 되돌려 같은 값을 연속 적용할 수 있게 한다.
            select.selectedIndex = 0;
        });
        return select;
    }

    /**
     * 색상 선택 input(폰트 색·배경색)을 툴바 라벨과 함께 생성한다.
     *
     * @param {string} label 라벨 텍스트(예: "글자색").
     * @param {string} title 접근성 라벨.
     * @param {string} defaultColor 초기 색상값(예: "#000000").
     * @param {function(string): void} onPick 색상 선택 시 콜백.
     * @returns {HTMLLabelElement} input 을 감싼 라벨 요소.
     */
    function createColorControl(label, title, defaultColor, onPick) {
        const wrapper = document.createElement("label");
        wrapper.className = "rich-text-editor__color";
        wrapper.title = title;

        const text = document.createElement("span");
        text.className = "rich-text-editor__color-label";
        text.textContent = label;

        const input = document.createElement("input");
        input.type = "color";
        input.value = defaultColor;
        input.setAttribute("aria-label", title);
        input.addEventListener("input", function () {
            onPick(input.value);
        });

        wrapper.appendChild(text);
        wrapper.appendChild(input);
        return wrapper;
    }

    /**
     * 단일 textarea 를 리치 텍스트 에디터로 초기화한다.
     *
     * @param {HTMLTextAreaElement} textarea 대상 textarea(data-rich-editor).
     */
    function initEditor(textarea) {
        // 이미 초기화된 경우 중복 생성 방지.
        if (textarea.dataset.richEditorReady === "1") {
            return;
        }
        textarea.dataset.richEditorReady = "1";

        const form = textarea.closest("form");

        // ── 편집 영역(contenteditable) 구성 ──────────────────────────────
        const container = document.createElement("div");
        container.className = "rich-text-editor";

        const toolbar = document.createElement("div");
        toolbar.className = "rich-text-editor__toolbar";
        toolbar.setAttribute("role", "toolbar");
        toolbar.setAttribute("aria-label", "본문 서식 도구");

        const editable = document.createElement("div");
        editable.className = "rich-text-editor__content rich-text-content";
        editable.setAttribute("contenteditable", "true");
        editable.setAttribute("role", "textbox");
        editable.setAttribute("aria-multiline", "true");
        editable.setAttribute("aria-label", "본문 편집 영역");

        // 초기 본문 로드 — 수정 화면의 기존 저장 본문을 에디터에 채운다.
        const initialFormat = textarea.dataset.initialFormat || "plain";
        const initialValue = textarea.value || "";
        if (initialValue) {
            if (initialFormat === "html") {
                editable.innerHTML = initialValue; // 저장 시 정화된 안전한 HTML.
            } else {
                editable.innerHTML = plainTextToHtml(initialValue);
            }
        }

        // execCommand 가 인라인 CSS(<span style>) 대신 태그(<b>, <font>)를 쓰도록
        // styleWithCSS 를 끈다 — 서버 allowlist 가 font 태그/속성을 보존하므로
        // 서식이 통째로 사라지지 않는다.
        try {
            document.execCommand("styleWithCSS", false, "false");
        } catch (error) {
            // 일부 브라우저는 미지원 — 무시하고 진행한다.
        }

        /**
         * 편집 영역에 포커스를 두고 execCommand 를 실행한다.
         *
         * 툴바 버튼 클릭 시 selection 이 풀리지 않도록 먼저 focus 한다.
         *
         * @param {string} command execCommand 명령.
         * @param {string=} value 명령 인자(선택).
         */
        function runCommand(command, value) {
            editable.focus();
            document.execCommand(command, false, value);
            syncToTextarea();
        }

        /**
         * 편집 영역의 현재 HTML 을 숨겨진 textarea 로 동기화한다.
         */
        function syncToTextarea() {
            textarea.value = editable.innerHTML;
        }

        // ── 툴바 버튼 구성 ──────────────────────────────────────────────
        // 1) 텍스트 서식
        toolbar.appendChild(
            createToolbarButton("B", "굵게", function () {
                runCommand("bold");
            })
        );
        toolbar.appendChild(
            createToolbarButton("I", "기울임", function () {
                runCommand("italic");
            })
        );
        toolbar.appendChild(
            createToolbarButton("U", "밑줄", function () {
                runCommand("underline");
            })
        );
        toolbar.appendChild(
            createToolbarButton("S", "취소선", function () {
                runCommand("strikeThrough");
            })
        );

        // 2) 폰트 종류
        toolbar.appendChild(
            createToolbarSelect(
                "글꼴",
                [
                    { value: "", label: "글꼴" },
                    { value: "맑은 고딕", label: "맑은 고딕" },
                    { value: "굴림", label: "굴림" },
                    { value: "바탕", label: "바탕" },
                    { value: "돋움", label: "돋움" },
                    { value: "궁서", label: "궁서" },
                    { value: "Arial", label: "Arial" },
                    { value: "Times New Roman", label: "Times New Roman" },
                    { value: "Courier New", label: "Courier New" },
                ],
                function (fontName) {
                    runCommand("fontName", fontName);
                }
            )
        );

        // 3) 폰트 크기 (execCommand fontSize 는 1~7 단계)
        toolbar.appendChild(
            createToolbarSelect(
                "글자 크기",
                [
                    { value: "", label: "크기" },
                    { value: "1", label: "아주 작게" },
                    { value: "2", label: "작게" },
                    { value: "3", label: "보통" },
                    { value: "4", label: "조금 크게" },
                    { value: "5", label: "크게" },
                    { value: "6", label: "더 크게" },
                    { value: "7", label: "아주 크게" },
                ],
                function (size) {
                    runCommand("fontSize", size);
                }
            )
        );

        // 4) 색상 — 글자색 / 배경색(형광펜)
        toolbar.appendChild(
            createColorControl("글자색", "글자 색상", "#000000", function (color) {
                runCommand("foreColor", color);
            })
        );
        toolbar.appendChild(
            createColorControl("배경", "글자 배경 색상", "#ffff00", function (color) {
                // hiliteColor 미지원 브라우저 대비 backColor fallback.
                editable.focus();
                if (!document.execCommand("hiliteColor", false, color)) {
                    document.execCommand("backColor", false, color);
                }
                syncToTextarea();
            })
        );

        // 5) 목록 / 들여쓰기
        toolbar.appendChild(
            createToolbarButton("• 목록", "글머리 기호 목록", function () {
                runCommand("insertUnorderedList");
            })
        );
        toolbar.appendChild(
            createToolbarButton("1. 목록", "번호 매기기 목록", function () {
                runCommand("insertOrderedList");
            })
        );
        toolbar.appendChild(
            createToolbarButton("→|", "들여쓰기", function () {
                runCommand("indent");
            })
        );
        toolbar.appendChild(
            createToolbarButton("|←", "내어쓰기", function () {
                runCommand("outdent");
            })
        );

        // 6) 정렬
        toolbar.appendChild(
            createToolbarButton("좌", "왼쪽 정렬", function () {
                runCommand("justifyLeft");
            })
        );
        toolbar.appendChild(
            createToolbarButton("중", "가운데 정렬", function () {
                runCommand("justifyCenter");
            })
        );
        toolbar.appendChild(
            createToolbarButton("우", "오른쪽 정렬", function () {
                runCommand("justifyRight");
            })
        );

        // 7) 표 삽입
        toolbar.appendChild(
            createToolbarButton("표 삽입", "표 삽입", function () {
                insertTable();
            })
        );

        // 8) 서식 지우기
        toolbar.appendChild(
            createToolbarButton("서식 지우기", "선택 영역 서식 제거", function () {
                runCommand("removeFormat");
            })
        );

        /**
         * 사용자가 입력한 행/열 수만큼의 표를 편집 영역에 삽입한다.
         *
         * 표 테두리가 보이도록 인라인 style(border) 을 넣는다 — 서버 allowlist 가
         * table/td 의 style·border 속성을 보존하므로 저장 후에도 테두리가 남는다.
         */
        function insertTable() {
            const rowsRaw = window.prompt("표 행 수를 입력하세요 (1~20)", "2");
            if (rowsRaw === null) {
                return;
            }
            const columnsRaw = window.prompt("표 열 수를 입력하세요 (1~10)", "2");
            if (columnsRaw === null) {
                return;
            }

            const rows = Math.min(Math.max(parseInt(rowsRaw, 10) || 0, 1), 20);
            const columns = Math.min(Math.max(parseInt(columnsRaw, 10) || 0, 1), 10);

            let tableHtml =
                '<table style="border-collapse: collapse; width: 100%;" border="1">';
            for (let rowIndex = 0; rowIndex < rows; rowIndex += 1) {
                tableHtml += "<tr>";
                for (
                    let columnIndex = 0;
                    columnIndex < columns;
                    columnIndex += 1
                ) {
                    tableHtml +=
                        '<td style="border: 1px solid #999999; padding: 6px; min-width: 40px;">&nbsp;</td>';
                }
                tableHtml += "</tr>";
            }
            tableHtml += "</table><p><br></p>";

            editable.focus();
            document.execCommand("insertHTML", false, tableHtml);
            syncToTextarea();
        }

        // ── 입력/붙여넣기 → textarea 동기화 ──────────────────────────────
        // 일반 입력·붙여넣기마다 textarea 를 갱신한다. 붙여넣기는 기본 동작
        // (클립보드 HTML 보존)을 그대로 두고, 삽입이 끝난 다음 프레임에서 동기화
        // 한다 — Word/Outlook 서식(표·폰트)이 보존된 채 저장된다.
        editable.addEventListener("input", syncToTextarea);
        editable.addEventListener("paste", function () {
            window.setTimeout(syncToTextarea, 0);
        });

        // ── DOM 배치: textarea 자리에 에디터를 끼워 넣고 textarea 는 숨긴다 ──
        container.appendChild(toolbar);
        container.appendChild(editable);
        textarea.parentNode.insertBefore(container, textarea);

        textarea.style.display = "none";
        textarea.setAttribute("aria-hidden", "true");
        textarea.tabIndex = -1;
        // 숨겨진 textarea 가 native required/maxlength 검증으로 제출을 막지 않도록
        // 제거한다(검증은 제출 핸들러에서 직접 수행).
        textarea.removeAttribute("required");
        textarea.removeAttribute("maxlength");

        // 같은 form 안의 body_format 을 'html' 로 강제(라우트 계약 일치).
        ensureBodyFormatHtml(form, textarea);

        // 최초 1회 동기화(수정 화면의 초기 로드 본문 반영).
        syncToTextarea();

        // ── 제출 직전 최종 동기화 + 빈 본문 검증 ─────────────────────────
        if (form) {
            form.addEventListener("submit", function (event) {
                syncToTextarea();
                // 보이는 콘텐츠가 전혀 없으면(빈 글) 제출을 막는다 — 표는 콘텐츠로
                // 인정(서버 _has_visible_content 와 동일 정책).
                const hasText = editable.textContent.trim().length > 0;
                const hasTable = editable.querySelector("table") !== null;
                const hasImage = editable.querySelector("img") !== null;
                if (!hasText && !hasTable && !hasImage) {
                    event.preventDefault();
                    window.alert("본문을 입력하세요.");
                    editable.focus();
                }
            });
        }
    }

    /**
     * 같은 form 안에 body_format 필드가 'html' 값으로 존재하도록 보장한다.
     *
     * 템플릿에 hidden input(name=body_format)이 이미 있으면 값을 'html' 로
     * 맞추고, 없으면 새로 만들어 textarea 뒤에 추가한다.
     *
     * @param {HTMLFormElement|null} form 대상 form.
     * @param {HTMLTextAreaElement} textarea 기준 textarea.
     */
    function ensureBodyFormatHtml(form, textarea) {
        if (!form) {
            return;
        }
        let bodyFormatInput = form.querySelector("input[name=body_format]");
        if (!bodyFormatInput) {
            bodyFormatInput = document.createElement("input");
            bodyFormatInput.type = "hidden";
            bodyFormatInput.name = "body_format";
            textarea.parentNode.insertBefore(bodyFormatInput, textarea.nextSibling);
        }
        bodyFormatInput.value = "html";
    }

    /**
     * 페이지 안의 모든 data-rich-editor textarea 를 초기화한다.
     */
    function initAll() {
        const targets = document.querySelectorAll("textarea[data-rich-editor]");
        targets.forEach(function (textarea) {
            initEditor(textarea);
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initAll);
    } else {
        initAll();
    }
})();
