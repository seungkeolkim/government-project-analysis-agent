"""백업 서비스 통합 테스트 (task 00094-1).

테스트 범위:
    - SystemSetting CRUD (get_setting / set_setting)
    - run_backup: 실제 SQLite 파일을 대상으로 백업 실행 및 이력 기록 검증
    - _prune_old_backups: 보관 수 초과 시 오래된 파일 삭제 검증
    - list_backup_files: 백업 파일 목록 반환 검증
    - list_backup_history: 이력 조회 검증

주의:
    sqlite3.Connection.backup() 의 source 는 in-memory SQLite 가 아닌
    파일 기반 SQLite 를 요구한다. 따라서 모든 테스트는 tmp_path 기반
    파일 SQLite 를 사용한다 (conftest.py 의 test_engine 픽스처 참조).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.backup.constants import (
    DEFAULT_BACKUP_CRON,
    DEFAULT_BACKUP_MAX_COUNT,
    SETTING_KEY_BACKUP_CRON,
    SETTING_KEY_BACKUP_MAX_COUNT,
)
from app.backup.service import (
    _prune_old_backups,
    get_backup_settings,
    get_setting,
    list_backup_files,
    list_backup_history,
    run_backup,
    set_setting,
)
from app.db.models import BackupHistory


# ──────────────────────────────────────────────────────────────
# 보조 헬퍼
# ──────────────────────────────────────────────────────────────


def _make_dummy_sqlite(path: Path) -> None:
    """테스트용 더미 SQLite 파일을 생성한다."""
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE IF NOT EXISTS dummy (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()


# ──────────────────────────────────────────────────────────────
# SystemSetting CRUD 테스트
# ──────────────────────────────────────────────────────────────


class TestSystemSettingCrud:
    """get_setting / set_setting 테스트."""

    def test_get_setting_returns_none_for_missing_key(self, db_session: Session) -> None:
        """존재하지 않는 키는 None 을 반환해야 한다."""
        result = get_setting(db_session, "nonexistent.key")
        assert result is None

    def test_set_and_get_setting(self, db_session: Session) -> None:
        """set 후 get 하면 저장한 값이 반환돼야 한다."""
        set_setting(db_session, SETTING_KEY_BACKUP_CRON, "0 2 * * *")
        db_session.flush()

        result = get_setting(db_session, SETTING_KEY_BACKUP_CRON)
        assert result == "0 2 * * *"

    def test_set_setting_updates_existing_key(self, db_session: Session) -> None:
        """이미 존재하는 키에 set 하면 값이 갱신돼야 한다."""
        set_setting(db_session, SETTING_KEY_BACKUP_MAX_COUNT, "5")
        db_session.flush()
        set_setting(db_session, SETTING_KEY_BACKUP_MAX_COUNT, "10")
        db_session.flush()

        result = get_setting(db_session, SETTING_KEY_BACKUP_MAX_COUNT)
        assert result == "10"

    def test_set_setting_none_value(self, db_session: Session) -> None:
        """None 값을 저장하면 get 도 None 이어야 한다."""
        set_setting(db_session, "some.key", None)
        db_session.flush()

        result = get_setting(db_session, "some.key")
        assert result is None

    def test_get_backup_settings_returns_defaults_when_not_set(self, db_session: Session) -> None:
        """SystemSetting 에 값이 없으면 기본값을 반환해야 한다."""
        settings = get_backup_settings(db_session)
        assert settings["cron_expression"] == DEFAULT_BACKUP_CRON
        assert settings["max_count"] == str(DEFAULT_BACKUP_MAX_COUNT)


# ──────────────────────────────────────────────────────────────
# run_backup 테스트
# ──────────────────────────────────────────────────────────────


class TestRunBackup:
    """run_backup 통합 테스트."""

    def test_run_backup_creates_backup_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, test_engine: object
    ) -> None:
        """백업 실행 시 backup_dir 에 파일이 생성돼야 한다."""
        src_db = tmp_path / "source.sqlite3"
        backup_dir = tmp_path / "backups"
        _make_dummy_sqlite(src_db)

        # get_settings() 를 패치해 db_url 과 backup_dir 을 tmp_path 로 연결
        monkeypatch.setenv("DB_URL", f"sqlite:///{tmp_path / 'test.sqlite3'}")
        monkeypatch.setenv("BACKUP_DIR", str(backup_dir))

        # backup_dir 와 db 타겟을 직접 주입하기 위해 service 내부 함수 패치
        monkeypatch.setattr(
            "app.backup.service.get_backup_db_targets",
            lambda: [src_db],
        )
        monkeypatch.setattr(
            "app.backup.service._get_backup_dir",
            lambda: backup_dir,
        )
        monkeypatch.setattr(
            "app.backup.service._get_max_count_from_db",
            lambda: DEFAULT_BACKUP_MAX_COUNT,
        )

        history = run_backup(trigger="manual")

        assert history.success is True
        assert history.trigger == "manual"
        assert len(history.backup_files) == 1
        assert history.total_size_bytes is not None and history.total_size_bytes > 0
        assert history.error_message is None
        assert history.duration_seconds is not None

        # 실제 파일이 존재하는지 확인
        backup_files_in_dir = list(backup_dir.iterdir())
        assert len(backup_files_in_dir) == 1
        assert backup_files_in_dir[0].suffix == ".sqlite3"

    def test_run_backup_records_history(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_session: Session, test_engine: object
    ) -> None:
        """백업 후 BackupHistory 에 이력이 기록돼야 한다."""
        src_db = tmp_path / "source.sqlite3"
        backup_dir = tmp_path / "backups"
        _make_dummy_sqlite(src_db)

        monkeypatch.setattr("app.backup.service.get_backup_db_targets", lambda: [src_db])
        monkeypatch.setattr("app.backup.service._get_backup_dir", lambda: backup_dir)
        monkeypatch.setattr("app.backup.service._get_max_count_from_db", lambda: DEFAULT_BACKUP_MAX_COUNT)

        run_backup(trigger="scheduled")

        # 새 세션으로 조회
        from sqlalchemy import select
        rows = db_session.scalars(select(BackupHistory)).all()
        assert len(rows) == 1
        assert rows[0].trigger == "scheduled"
        assert rows[0].success is True

    def test_run_backup_skips_nonexistent_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, test_engine: object
    ) -> None:
        """존재하지 않는 DB 파일은 건너뛰고 성공으로 처리해야 한다."""
        nonexistent = tmp_path / "missing.sqlite3"
        backup_dir = tmp_path / "backups"

        monkeypatch.setattr("app.backup.service.get_backup_db_targets", lambda: [nonexistent])
        monkeypatch.setattr("app.backup.service._get_backup_dir", lambda: backup_dir)
        monkeypatch.setattr("app.backup.service._get_max_count_from_db", lambda: DEFAULT_BACKUP_MAX_COUNT)

        history = run_backup(trigger="manual")

        # 대상 파일이 없어도 실패로 처리하지 않는다
        assert history.success is True
        assert len(history.backup_files) == 0
        assert len(history.target_files) == 0


# ──────────────────────────────────────────────────────────────
# _prune_old_backups 테스트
# ──────────────────────────────────────────────────────────────


class TestPruneOldBackups:
    """_prune_old_backups 단위 테스트."""

    def _make_backup_file(self, backup_dir: Path, stem: str, timestamp: str) -> Path:
        """더미 백업 파일을 생성한다. 파일명: {stem}_{timestamp}.sqlite3"""
        f = backup_dir / f"{stem}_{timestamp}.sqlite3"
        f.write_bytes(b"dummy")
        return f

    def test_prune_removes_oldest_groups_beyond_max(self, tmp_path: Path) -> None:
        """max_count 를 초과한 오래된 타임스탬프 그룹이 삭제돼야 한다."""
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()

        # 타임스탬프 3개 그룹 (각 2파일씩) 생성
        for ts in ["20260506_030000", "20260507_030000", "20260508_030000"]:
            self._make_backup_file(backup_dir, "app", ts)
            self._make_backup_file(backup_dir, "boards", ts)

        # max_count=2: 최신 2개 그룹만 유지, 1개 그룹 삭제
        deleted = _prune_old_backups(backup_dir, max_count=2)

        assert deleted == 2  # 가장 오래된 그룹(20260506) 2파일 삭제
        remaining = [f.name for f in backup_dir.iterdir()]
        assert all("20260506" not in name for name in remaining)
        assert any("20260507" in name for name in remaining)
        assert any("20260508" in name for name in remaining)

    def test_prune_no_deletion_when_within_limit(self, tmp_path: Path) -> None:
        """보관 수 이하이면 삭제가 발생하지 않아야 한다."""
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()

        self._make_backup_file(backup_dir, "app", "20260508_030000")

        deleted = _prune_old_backups(backup_dir, max_count=7)
        assert deleted == 0

    def test_prune_with_max_count_zero_does_nothing(self, tmp_path: Path) -> None:
        """max_count=0 이면 삭제를 하지 않는다."""
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        self._make_backup_file(backup_dir, "app", "20260508_030000")

        deleted = _prune_old_backups(backup_dir, max_count=0)
        assert deleted == 0

    def test_prune_nonexistent_dir_returns_zero(self, tmp_path: Path) -> None:
        """백업 디렉터리가 없으면 0 을 반환한다."""
        deleted = _prune_old_backups(tmp_path / "nonexistent", max_count=5)
        assert deleted == 0


# ──────────────────────────────────────────────────────────────
# list_backup_files 테스트
# ──────────────────────────────────────────────────────────────


class TestListBackupFiles:
    """list_backup_files 단위 테스트."""

    def test_list_returns_files_newest_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """파일이 수정 시각 내림차순으로 반환돼야 한다."""
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()

        older = backup_dir / "app_20260507_030000.sqlite3"
        newer = backup_dir / "app_20260508_030000.sqlite3"
        older.write_bytes(b"old")
        newer.write_bytes(b"new")

        # mtime 차이를 명시적으로 설정 (older < newer)
        import os
        os.utime(older, (1000.0, 1000.0))
        os.utime(newer, (2000.0, 2000.0))

        monkeypatch.setattr("app.backup.service._get_backup_dir", lambda: backup_dir)

        files = list_backup_files()
        assert len(files) == 2
        assert files[0]["filename"] == newer.name
        assert files[1]["filename"] == older.name

    def test_list_returns_empty_when_dir_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """백업 디렉터리가 없으면 빈 리스트를 반환해야 한다."""
        monkeypatch.setattr(
            "app.backup.service._get_backup_dir",
            lambda: tmp_path / "nonexistent",
        )
        assert list_backup_files() == []

    def test_list_file_dict_contains_expected_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """반환된 dict 에 filename, size_bytes, modified_at 키가 있어야 한다."""
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        (backup_dir / "app_20260508_030000.sqlite3").write_bytes(b"x" * 100)

        monkeypatch.setattr("app.backup.service._get_backup_dir", lambda: backup_dir)

        files = list_backup_files()
        assert len(files) == 1
        assert set(files[0].keys()) == {"filename", "size_bytes", "modified_at"}
        assert files[0]["size_bytes"] == 100
        assert isinstance(files[0]["modified_at"], datetime)
        assert files[0]["modified_at"].tzinfo is not None


# ──────────────────────────────────────────────────────────────
# list_backup_history 테스트
# ──────────────────────────────────────────────────────────────


class TestListBackupHistory:
    """list_backup_history 테스트."""

    def test_returns_history_newest_first(self, db_session: Session, test_engine: object) -> None:
        """이력이 executed_at 내림차순(최신 먼저)으로 반환돼야 한다."""
        older = BackupHistory(
            executed_at=datetime(2026, 5, 7, 3, 0, tzinfo=UTC),
            trigger="scheduled",
            target_files=[],
            backup_files=[],
            success=True,
        )
        newer = BackupHistory(
            executed_at=datetime(2026, 5, 8, 3, 0, tzinfo=UTC),
            trigger="manual",
            target_files=[],
            backup_files=[],
            success=True,
        )
        db_session.add_all([older, newer])
        db_session.flush()

        rows = list_backup_history(db_session, limit=10)
        assert rows[0].executed_at > rows[1].executed_at

    def test_limit_parameter_is_respected(self, db_session: Session, test_engine: object) -> None:
        """limit 파라미터만큼만 반환돼야 한다."""
        for i in range(5):
            db_session.add(
                BackupHistory(
                    executed_at=datetime(2026, 5, i + 1, 3, 0, tzinfo=UTC),
                    trigger="scheduled",
                    target_files=[],
                    backup_files=[],
                    success=True,
                )
            )
        db_session.flush()

        rows = list_backup_history(db_session, limit=3)
        assert len(rows) == 3
