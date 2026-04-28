"""고아 첨부 파일 GC 단위/통합 테스트 (Phase 5a / task 00041-5).

검증 대상:
    - ``app/scrape_control/orphan_gc.py`` 의 핵심 함수 (compute_orphan_files /
      gather_db_attachment_paths / delete_orphan_files / run_gc).
    - ``scripts/gc_orphan_attachments.py`` 의 CLI 종료 코드 분기.

설계 §11.5 의 검증 기준 (사용자 원문 검증 8):
    - --dry-run 으로 후보 출력 → 운영자가 검수 → 실제 실행.
    - **본 테이블 참조가 있는 파일은 절대 삭제되지 않아야 한다**.
    - ScrapeRun running 가드가 동작해야 한다 (--force 없이 거부).

규약:
    - tmp_path 기반 격리. db_session fixture 로 깨끗한 SQLite 사용.
    - CLI 분기는 importlib 로 스크립트 모듈을 로드해 ``main()`` 을 직접 호출
      (subprocess 대신 — tests/auth/test_create_admin.py 와 동일 패턴).
"""

from __future__ import annotations

import importlib.util
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Iterator

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Announcement, AnnouncementStatus, Attachment, ScrapeRun
from app.scrape_control.orphan_gc import (
    EXIT_ENV_ERROR,
    EXIT_OK,
    EXIT_SCRAPE_RUNNING,
    OrphanGcReport,
    collect_disk_files,
    compute_orphan_files,
    delete_orphan_files,
    gather_db_attachment_paths,
    run_gc,
)


_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


# ──────────────────────────────────────────────────────────────
# fixture / helpers
# ──────────────────────────────────────────────────────────────


def _make_announcement(
    session: Session,
    *,
    source_id: str,
    title: str = "테스트 공고",
) -> Announcement:
    """본 테이블 attachments 의 FK target 으로 쓸 Announcement 1 건 생성."""
    announcement = Announcement(
        source_announcement_id=source_id,
        source_type="IRIS",
        title=title,
        status=AnnouncementStatus.RECEIVING,
        is_current=True,
    )
    session.add(announcement)
    session.flush()
    return announcement


def _make_attachment_with_file(
    session: Session,
    *,
    announcement_id: int,
    root: Path,
    filename: str,
    content: bytes = b"x" * 10,
    sha256: str | None = None,
) -> tuple[Attachment, Path]:
    """디스크에 파일을 만들고 본 테이블 attachments 에 row 를 INSERT 한다.

    Returns:
        (Attachment ORM, 디스크 파일 절대경로 (resolve 적용)).
    """
    file_path = root / filename
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(content)
    resolved = file_path.resolve(strict=False)

    attachment = Attachment(
        announcement_id=announcement_id,
        original_filename=filename.split("/")[-1],
        stored_path=str(resolved),
        file_ext=filename.rsplit(".", 1)[-1].lower(),
        file_size=len(content),
        sha256=sha256 or "a" * 64,
        downloaded_at=datetime.now(tz=UTC),
    )
    session.add(attachment)
    session.flush()
    return attachment, resolved


def _create_orphan_file(root: Path, relative: str, content: bytes = b"orphan") -> Path:
    """root 아래에 디스크 파일만 만들고 DB row 는 만들지 않는다 (= 고아)."""
    file_path = root / relative
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(content)
    return file_path.resolve(strict=False)


@pytest.fixture
def gc_root(tmp_path: Path) -> Path:
    """GC 가 스캔할 격리된 download_dir.

    ``tmp_path`` 자체에는 conftest 가 ``test.sqlite3`` 를 둬서 GC 의 disk 스캔
    결과에 섞여 들어간다 (테스트 의도와 무관). 별도 하위 디렉터리를 만들어
    GC root 로 사용해 SQLite 파일과 분리한다.
    """
    root = tmp_path / "downloads"
    root.mkdir(parents=True, exist_ok=True)
    return root


@contextmanager
def _session_factory_yielding(session: Session) -> Iterator[Session]:
    """run_gc 에 주입할 'session_scope_factory' 호환 컨텍스트 매니저.

    pytest 의 ``db_session`` 을 단일 컨텍스트 매니저로 감싸서 run_gc 가 with
    문으로 열고 닫을 수 있게 한다 — 실제로는 같은 session 을 반환만 한다.
    """
    yield session


def _make_factory(session: Session):
    """run_gc(session_scope_factory=...) 에 넘길 callable 을 만든다."""
    return lambda: _session_factory_yielding(session)


# ──────────────────────────────────────────────────────────────
# 핵심 함수 단위 테스트
# ──────────────────────────────────────────────────────────────


def test_collect_disk_files_returns_only_files_recursively(tmp_path: Path) -> None:
    """심볼릭 링크 / 디렉터리는 yield 하지 않고 일반 파일만 yield 한다."""
    (tmp_path / "a.pdf").write_bytes(b"a")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.hwp").write_bytes(b"b")
    (tmp_path / "sub" / "deep").mkdir()
    (tmp_path / "sub" / "deep" / "c.zip").write_bytes(b"c")
    # 빈 디렉터리는 yield 안 됨.
    (tmp_path / "empty_dir").mkdir()

    result = sorted(collect_disk_files(tmp_path))
    expected_names = {"a.pdf", "b.hwp", "c.zip"}
    assert {p.name for p in result} == expected_names
    assert all(p.is_file() for p in result)


def test_collect_disk_files_handles_missing_root(tmp_path: Path) -> None:
    """root 가 존재하지 않으면 yield 0건 (예외 없이)."""
    missing = tmp_path / "does_not_exist"
    assert list(collect_disk_files(missing)) == []


def test_compute_orphan_files_excludes_db_referenced_paths(tmp_path: Path) -> None:
    """DB 참조 set 에 포함된 path 는 고아로 판정되지 않는다."""
    file1 = _create_orphan_file(tmp_path, "a.pdf")
    file2 = _create_orphan_file(tmp_path, "sub/b.pdf")
    file3 = _create_orphan_file(tmp_path, "c.pdf")

    db_paths = {file1, file3}
    orphans, total = compute_orphan_files(tmp_path.resolve(strict=False), db_paths)
    assert total == 3
    assert orphans == [file2]


def test_compute_orphan_files_returns_sorted_list(tmp_path: Path) -> None:
    """결과는 path asc 정렬이라 재현 가능하다."""
    paths = [_create_orphan_file(tmp_path, name) for name in ["z.pdf", "a.pdf", "m.pdf"]]
    orphans, _ = compute_orphan_files(tmp_path.resolve(strict=False), set())
    assert orphans == sorted(paths)


def test_compute_orphan_files_skips_paths_outside_root(tmp_path: Path) -> None:
    """root.is_relative_to 가드 — root 외부 경로가 disk set 에 끼어들어도 제외."""
    # root 안의 정상 고아.
    inside_orphan = _create_orphan_file(tmp_path, "inside.pdf")
    # root 밖 디렉터리에 파일을 만들어 db_paths 에는 넣어보지만 — disk 스캔은
    # root 내부만 yield 하므로 이 파일은 결과에 안 잡혀야 한다.
    outside_dir = tmp_path.parent / "outside"
    outside_dir.mkdir(exist_ok=True)
    (outside_dir / "outside.pdf").write_bytes(b"o")

    orphans, _ = compute_orphan_files(tmp_path.resolve(strict=False), set())
    assert orphans == [inside_orphan]


def test_gather_db_attachment_paths_normalizes_and_skips_null(
    db_session: Session, gc_root: Path
) -> None:
    """본 테이블의 stored_path 가 정규화되어 set 으로 반환된다 (NULL 스킵)."""
    announcement = _make_announcement(db_session, source_id="A-001")
    _make_attachment_with_file(
        db_session,
        announcement_id=announcement.id,
        root=gc_root,
        filename="x.pdf",
    )
    # NULL stored_path 는 회귀 안전망 — 다운로드 실패 가설.
    null_attachment = Attachment(
        announcement_id=announcement.id,
        original_filename="lost.pdf",
        stored_path="",  # 빈 문자열
        file_ext="pdf",
        downloaded_at=datetime.now(tz=UTC),
    )
    db_session.add(null_attachment)
    db_session.flush()
    db_session.commit()

    db_paths = gather_db_attachment_paths(db_session)
    assert len(db_paths) == 1
    assert (gc_root / "x.pdf").resolve(strict=False) in db_paths


def test_delete_orphan_files_unlinks_and_cleans_empty_dirs(tmp_path: Path) -> None:
    """삭제 + 빈 디렉터리 cleanup. 부모-자식 관계가 있어도 bottom-up 으로 정리."""
    deep = _create_orphan_file(tmp_path, "a/b/c/d.pdf")
    sibling = _create_orphan_file(tmp_path, "a/sibling.pdf")
    # 이 디렉터리는 "비어 있음" 이 아니라 다른 파일이 남으므로 정리되면 안 됨.
    other_keep = _create_orphan_file(tmp_path, "a/keep.pdf")

    deleted, failed, removed_dirs = delete_orphan_files(
        [deep, sibling], tmp_path.resolve(strict=False)
    )
    assert deleted == 2
    assert failed == []
    # a/b/c → a/b → (a 는 keep 가 남아있어 정리 안 됨). 즉 2개 정리.
    assert removed_dirs == 2
    # 안 지운 파일은 그대로.
    assert other_keep.exists()
    # root 자체는 비워도 절대 삭제 안 됨.
    assert tmp_path.exists()


def test_delete_orphan_files_skips_paths_outside_root(tmp_path: Path) -> None:
    """root 외부 경로는 절대 unlink 하지 않는다 (defense-in-depth)."""
    inside = _create_orphan_file(tmp_path, "in.pdf")
    outside_dir = tmp_path.parent / "out_test_skip"
    outside_dir.mkdir(exist_ok=True)
    outside_file = outside_dir / "out.pdf"
    outside_file.write_bytes(b"o")
    outside_resolved = outside_file.resolve(strict=False)

    try:
        deleted, failed, _ = delete_orphan_files(
            [inside, outside_resolved], tmp_path.resolve(strict=False)
        )
        assert deleted == 1
        assert not inside.exists()
        # 외부 파일은 보존.
        assert outside_resolved.exists()
        assert len(failed) == 1
        assert failed[0][0] == outside_resolved
        assert "root 외부" in failed[0][1]
    finally:
        outside_file.unlink(missing_ok=True)
        outside_dir.rmdir()


# ──────────────────────────────────────────────────────────────
# run_gc — end-to-end (DB + 디스크)
# ──────────────────────────────────────────────────────────────


def test_run_gc_dry_run_does_not_delete(db_session: Session, gc_root: Path) -> None:
    """dry_run=True 면 보고서만 산출하고 디스크는 변경하지 않는다."""
    announcement = _make_announcement(db_session, source_id="A-001")
    _, kept_path = _make_attachment_with_file(
        db_session, announcement_id=announcement.id, root=gc_root, filename="keep.pdf"
    )
    orphan_path = _create_orphan_file(gc_root, "orphan.pdf", content=b"abc")
    db_session.commit()

    report = run_gc(
        dry_run=True,
        root_override=gc_root,
        session_scope_factory=_make_factory(db_session),
    )

    assert report.dry_run is True
    assert report.skipped_due_to_running_scrape_run is False
    assert report.disk_file_count == 2
    assert report.db_referenced_count == 1
    assert report.orphan_files == [orphan_path]
    assert report.deleted_count == 0
    # 디스크는 그대로.
    assert kept_path.exists()
    assert orphan_path.exists()


def test_run_gc_apply_deletes_only_orphans(db_session: Session, gc_root: Path) -> None:
    """dry_run=False 면 고아만 unlink 하고 본 테이블 참조 파일은 보존 (검증 8 핵심)."""
    announcement = _make_announcement(db_session, source_id="A-001")
    _, kept_path = _make_attachment_with_file(
        db_session, announcement_id=announcement.id, root=gc_root, filename="keep.pdf"
    )
    orphan1 = _create_orphan_file(gc_root, "orphan1.pdf", content=b"o1")
    orphan2 = _create_orphan_file(gc_root, "sub/orphan2.pdf", content=b"o2")
    db_session.commit()

    report = run_gc(
        dry_run=False,
        root_override=gc_root,
        session_scope_factory=_make_factory(db_session),
    )

    assert report.deleted_count == 2
    assert report.deletion_failed == []
    # DB 참조 파일은 절대 삭제되지 않아야 한다 (사용자 원문 검증 8).
    assert kept_path.exists()
    # 고아 파일은 모두 삭제.
    assert not orphan1.exists()
    assert not orphan2.exists()


def test_run_gc_skips_when_scrape_run_is_running(
    db_session: Session, gc_root: Path
) -> None:
    """ScrapeRun running 이 있고 force=False 면 GC 가 거부된다 (검증 + 설계 §11.3)."""
    # running ScrapeRun row 를 직접 INSERT.
    running = ScrapeRun(
        started_at=datetime.now(tz=UTC),
        status="running",
        trigger="cli",
        source_counts={},
    )
    db_session.add(running)
    # 고아 파일도 만들어 둔다 — 거부되면 절대 삭제되면 안 됨.
    orphan_path = _create_orphan_file(gc_root, "orphan.pdf")
    db_session.commit()

    report = run_gc(
        dry_run=False,
        root_override=gc_root,
        session_scope_factory=_make_factory(db_session),
    )

    assert report.skipped_due_to_running_scrape_run is True
    assert report.deleted_count == 0
    # 고아 파일은 그대로 — 거부됐으므로.
    assert orphan_path.exists()


def test_run_gc_force_proceeds_despite_running_scrape_run(
    db_session: Session, gc_root: Path
) -> None:
    """force=True 면 ScrapeRun running 이어도 GC 가 진행된다."""
    db_session.add(
        ScrapeRun(
            started_at=datetime.now(tz=UTC),
            status="running",
            trigger="cli",
            source_counts={},
        )
    )
    orphan_path = _create_orphan_file(gc_root, "orphan.pdf")
    db_session.commit()

    report = run_gc(
        dry_run=False,
        root_override=gc_root,
        force=True,
        session_scope_factory=_make_factory(db_session),
    )

    assert report.skipped_due_to_running_scrape_run is False
    assert report.deleted_count == 1
    assert not orphan_path.exists()


def test_run_gc_returns_empty_report_when_no_orphans(
    db_session: Session, gc_root: Path
) -> None:
    """디스크 / DB 참조가 일치하면 고아 0건 — deleted=0 + 정상 종료."""
    announcement = _make_announcement(db_session, source_id="A-001")
    _make_attachment_with_file(
        db_session, announcement_id=announcement.id, root=gc_root, filename="only.pdf"
    )
    db_session.commit()

    report = run_gc(
        dry_run=False,
        root_override=gc_root,
        session_scope_factory=_make_factory(db_session),
    )
    assert report.orphan_files == []
    assert report.deleted_count == 0
    assert report.disk_file_count == 1
    assert report.db_referenced_count == 1


def test_run_gc_does_not_touch_files_outside_root(
    db_session: Session, gc_root: Path
) -> None:
    """root 외부의 파일은 GC 가 절대로 건드리지 않는다 (안전 가드)."""
    # gc_root 안에 고아 만들고, 옆 디렉터리에는 보존되어야 할 외부 파일을 만든다.
    inside_orphan = _create_orphan_file(gc_root, "inside.pdf")
    outside_dir = gc_root.parent / "outside_for_gc"
    outside_dir.mkdir(exist_ok=True)
    outside_file = outside_dir / "outside.pdf"
    outside_file.write_bytes(b"o")
    db_session.commit()

    try:
        run_gc(
            dry_run=False,
            root_override=gc_root,
            session_scope_factory=_make_factory(db_session),
        )
        # 외부 파일은 그대로 보존.
        assert outside_file.exists()
        assert not inside_orphan.exists()
    finally:
        outside_file.unlink(missing_ok=True)
        outside_dir.rmdir()


def test_run_gc_handles_missing_root_directory(
    db_session: Session, tmp_path: Path
) -> None:
    """root_override 가 없는 디렉터리면 보고서만 비어서 반환 (예외 없이)."""
    missing = tmp_path / "does_not_exist"
    db_session.commit()
    report = run_gc(
        dry_run=False,
        root_override=missing,
        session_scope_factory=_make_factory(db_session),
    )
    assert report.disk_file_count == 0
    assert report.deleted_count == 0


# ──────────────────────────────────────────────────────────────
# CLI 종료 코드 분기 (scripts/gc_orphan_attachments.py)
# ──────────────────────────────────────────────────────────────


def _load_gc_script_module() -> ModuleType:
    """``scripts/gc_orphan_attachments.py`` 를 임의 이름 모듈로 로드한다.

    scripts/ 가 패키지가 아니라 importlib 로 직접 경로 지정 — tests/auth/
    test_create_admin.py 와 동일 패턴.
    """
    module_name = "scripts_gc_orphan_attachments_under_test"
    if module_name in sys.modules:
        return sys.modules[module_name]
    script_path = _PROJECT_ROOT / "scripts" / "gc_orphan_attachments.py"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_cli_exits_with_code_2_when_scrape_run_is_running(
    db_session: Session,
    gc_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ScrapeRun running 가드가 동작해 sys.exit(2) — 설계 §11.2 종료 코드."""
    db_session.add(
        ScrapeRun(
            started_at=datetime.now(tz=UTC),
            status="running",
            trigger="cli",
            source_counts={},
        )
    )
    _create_orphan_file(gc_root, "orphan.pdf")
    db_session.commit()

    module = _load_gc_script_module()
    # argv 에 --root <gc_root> 만 주입 (--dry-run 없음 — 실제 삭제 시도하지만 가드로 막힘).
    monkeypatch.setattr(sys, "argv", [
        "gc_orphan_attachments.py",
        "--root", str(gc_root),
    ])
    with pytest.raises(SystemExit) as excinfo:
        module.main()
    assert excinfo.value.code == EXIT_SCRAPE_RUNNING


def test_cli_exits_with_code_0_on_normal_run(
    db_session: Session,
    gc_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """정상 실행은 sys.exit(0). dry-run / apply 둘 다 0."""
    announcement = _make_announcement(db_session, source_id="A-001")
    _make_attachment_with_file(
        db_session, announcement_id=announcement.id, root=gc_root, filename="keep.pdf"
    )
    _create_orphan_file(gc_root, "orphan.pdf")
    db_session.commit()

    module = _load_gc_script_module()

    # --dry-run 모드: 종료 코드 0, stdout 에 [DRY-RUN] 출력.
    monkeypatch.setattr(sys, "argv", [
        "gc_orphan_attachments.py",
        "--dry-run",
        "--root", str(gc_root),
    ])
    with pytest.raises(SystemExit) as excinfo:
        module.main()
    assert excinfo.value.code == EXIT_OK
    captured_dry = capsys.readouterr()
    assert "[DRY-RUN]" in captured_dry.out
    # dry-run 이라 디스크는 보존.
    assert (gc_root / "orphan.pdf").exists()

    # apply 모드: 같은 시나리오에서 실제 삭제 후 종료 코드 0.
    monkeypatch.setattr(sys, "argv", [
        "gc_orphan_attachments.py",
        "--root", str(gc_root),
    ])
    with pytest.raises(SystemExit) as excinfo:
        module.main()
    assert excinfo.value.code == EXIT_OK
    captured_apply = capsys.readouterr()
    assert "[APPLY]" in captured_apply.out
    # 고아 파일은 삭제됨.
    assert not (gc_root / "orphan.pdf").exists()


def test_cli_zero_orphans_exits_zero(
    db_session: Session,
    gc_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """고아 0건이어도 정상 (종료 코드 0)."""
    db_session.commit()
    module = _load_gc_script_module()
    monkeypatch.setattr(sys, "argv", [
        "gc_orphan_attachments.py",
        "--dry-run",
        "--root", str(gc_root),
    ])
    with pytest.raises(SystemExit) as excinfo:
        module.main()
    assert excinfo.value.code == EXIT_OK
