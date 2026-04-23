"""sources.yaml 편집기 서비스 (Phase 2 / 00025-5).

관리자 페이지 [sources.yaml] 탭이 호출하는 도메인 로직. 웹 계층은 HTTP 레이어만
담당하고, 파일 I/O·YAML/Pydantic 검증·백업·원자적 쓰기는 모두 여기서 한다.

편집 흐름:
    1. ``load_sources_yaml_text()``  — bind mount 된 sources.yaml 원문 로드.
    2. 사용자가 textarea 에서 수정 후 POST 제출.
    3. ``validate_sources_yaml_text()`` — YAML 파싱 → Pydantic SourcesConfig.
       실패 시 ``SourcesYamlValidationError`` 로 detail 목록과 함께 raise.
    4. ``save_sources_yaml_text()``   — 백업(data/backups/sources/YYYYMMDD_HHMMSS.yaml)
       → 원자적 쓰기(NamedTemporaryFile + os.replace).

설계 주의:
    - 편집 대상 경로는 컨테이너 기준 ``/run/config/sources.yaml`` (호스트 바인드
      마운트 원본). entrypoint.sh 가 만드는 per-run 임시 복사본(SOURCES_CONFIG_PATH)
      과는 다르다 — 현재 실행 중인 subprocess 는 자기 임시 복사본을 이미 들고
      있으므로 편집기가 원본을 수정해도 그 실행에는 영향이 없고, **다음** 수집
      실행에 반영된다 (guidance 명시).
    - 원자적 쓰기: 같은 디렉터리의 NamedTemporaryFile → os.replace. bind mount 가
      파일 단위일 때 EXDEV 가 발생할 수 있어 fallback 으로 직접 overwrite +
      fsync 를 수행한다 (guidance 의 atomic 요구를 BEST EFFORT 로 준수).
    - 파일이 아예 없는 상태(최초 진입)에서도 빈 textarea 로 진입해 저장 가능해야
      한다 — load 는 빈 문자열을 반환하고, save 는 백업을 생성하지 않는다.
"""

from __future__ import annotations

import errno
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import yaml
from loguru import logger
from pydantic import ValidationError

from app.config import PROJECT_ROOT
from app.sources.config_schema import SourcesConfig

# ──────────────────────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────────────────────

# 컨테이너 안의 호스트 바인드 마운트 기본 경로. entrypoint.sh 와 동일 규약.
_DEFAULT_SOURCES_YAML_MOUNT: Final[Path] = Path("/run/config/sources.yaml")

# entrypoint.sh 가 SOURCES_YAML_MOUNT 환경변수로 override 를 허용하므로 동일
# 계약을 사용한다. 이 값은 편집기 전용으로 쓰며, load_sources_config() 가
# 참조하는 SOURCES_CONFIG_PATH 와는 **다른** 개념이다 (per-run 임시 복사본 vs
# 원본 bind mount). 혼동 방지용 주석.
SOURCES_YAML_MOUNT_ENV_VAR: Final[str] = "SOURCES_YAML_MOUNT"

# 백업 디렉터리 하위 경로 (PROJECT_ROOT/data 기준). guidance 및 사용자 원문 명시.
BACKUP_SUBPATH: Final[str] = "backups/sources"

# 백업 파일 이름 포맷. YYYYMMDD_HHMMSS.yaml — 정렬 친화적 (파일시스템 정렬
# = 시간 정렬).
BACKUP_FILENAME_PATTERN: Final[str] = "%Y%m%d_%H%M%S"


# ──────────────────────────────────────────────────────────────
# 예외 타입
# ──────────────────────────────────────────────────────────────


class SourcesYamlValidationError(ValueError):
    """sources.yaml 편집 내용이 유효하지 않을 때 발생.

    - YAML syntax 오류 (``yaml.YAMLError``)
    - Pydantic 스키마 위반 (``ValidationError``)

    Attributes:
        details: 사용자에게 그대로 노출할 세부 메시지 리스트.
                 첫 원소가 최상위 메시지, 이후가 필드 path 별 에러.
    """

    def __init__(self, message: str, details: list[str] | None = None) -> None:
        super().__init__(message)
        self.message: str = message
        self.details: list[str] = list(details or [])


# ──────────────────────────────────────────────────────────────
# 결과 dataclass
# ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SaveResult:
    """``save_sources_yaml_text`` 의 반환값.

    Attributes:
        target_path:  실제로 쓴 파일 경로.
        backup_path:  생성된 백업 파일 경로. 원본이 없었거나 읽을 수 없어
                      백업을 만들지 않은 경우 None.
        byte_count:   저장된 바이트 크기 (UI 안내용 — 옵션).
    """

    target_path: Path
    backup_path: Path | None
    byte_count: int


# ──────────────────────────────────────────────────────────────
# 경로 해석
# ──────────────────────────────────────────────────────────────


def get_sources_yaml_path() -> Path:
    """편집기가 읽고 쓸 sources.yaml 경로를 반환한다.

    우선순위:
        1. 환경변수 ``SOURCES_YAML_MOUNT`` 값 (테스트 주입용).
        2. 기본값 ``/run/config/sources.yaml`` (entrypoint.sh 와 동일 규약).

    주의: ``app.sources.config_schema.load_sources_config`` 가 참조하는
    ``SOURCES_CONFIG_PATH`` 는 entrypoint.sh 가 만드는 per-run 임시 복사본이며,
    여기와는 **다른** 경로다. 편집기는 항상 **원본 bind mount** 를 직접 수정해
    다음 실행에 반영되도록 한다.
    """
    override = os.environ.get(SOURCES_YAML_MOUNT_ENV_VAR, "").strip()
    if override:
        return Path(override)
    return _DEFAULT_SOURCES_YAML_MOUNT


def get_backup_root(*, data_dir: Path | None = None) -> Path:
    """백업 디렉터리 루트 경로.

    기본은 ``PROJECT_ROOT/data/backups/sources``. 테스트에서 data_dir 를
    주입하면 ``<data_dir>/backups/sources``.
    """
    base = data_dir if data_dir is not None else PROJECT_ROOT / "data"
    return base / BACKUP_SUBPATH


# ──────────────────────────────────────────────────────────────
# 로드
# ──────────────────────────────────────────────────────────────


def load_sources_yaml_text() -> str:
    """편집 대상 파일의 원문 텍스트를 반환한다.

    - 파일이 존재하지 않으면 빈 문자열. (호스트가 아직 sources.yaml 을 만들지
      않은 상태. 편집기가 처음 저장할 때 신규 생성된다.)
    - 읽기 중 OS 에러는 그대로 올려 UI 가 500 을 띄우도록 한다 (권한 문제 등).
    """
    target = get_sources_yaml_path()
    if not target.exists():
        logger.info(
            "sources.yaml 편집 로드: 파일이 존재하지 않아 빈 텍스트 반환 — path={}",
            target,
        )
        return ""
    return target.read_text(encoding="utf-8")


# ──────────────────────────────────────────────────────────────
# 검증
# ──────────────────────────────────────────────────────────────


def validate_sources_yaml_text(text: str) -> SourcesConfig:
    """텍스트를 YAML 파싱하고 Pydantic SourcesConfig 로 검증한다.

    성공하면 SourcesConfig 인스턴스를 반환 (호출자는 대개 버리는 값 — 저장
    단계에서 별도로 검증 중).

    Args:
        text: textarea 에서 제출된 원문.

    Returns:
        유효한 SourcesConfig.

    Raises:
        SourcesYamlValidationError: YAML syntax 또는 Pydantic 검증 실패.
            `.details` 에 사용자에게 노출할 세부 메시지 목록이 채워진다.
    """
    # 빈 파일은 빈 config 로 허용 — sources: [] 와 동일 취급. load_sources_config
    # 의 동작(raw is None → SourcesConfig()) 과 일치.
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        # yaml_error 에는 문제 위치(Mark) 가 포함될 수 있다. str(exc) 로 충분.
        raise SourcesYamlValidationError(
            message="YAML 구문 오류가 있습니다.",
            details=[str(exc)],
        ) from exc

    if parsed is None:
        parsed = {}

    try:
        return SourcesConfig.model_validate(parsed)
    except ValidationError as exc:
        # ValidationError.errors() 는 {loc, msg, type, ...} dict 의 리스트.
        # loc 는 tuple — path 를 사람이 읽기 좋게 join 한다.
        detail_lines: list[str] = []
        for err in exc.errors():
            loc_parts = err.get("loc", ())
            loc_text = ".".join(str(part) for part in loc_parts) if loc_parts else "(root)"
            msg_text = err.get("msg", "")
            err_type = err.get("type", "")
            type_suffix = f" [{err_type}]" if err_type else ""
            detail_lines.append(f"{loc_text}: {msg_text}{type_suffix}")
        raise SourcesYamlValidationError(
            message="Pydantic 스키마 검증에 실패했습니다.",
            details=detail_lines,
        ) from exc


# ──────────────────────────────────────────────────────────────
# 원자적 쓰기
# ──────────────────────────────────────────────────────────────


def _atomic_write(target: Path, content: str) -> None:
    """NamedTemporaryFile + os.replace 로 대상 파일을 원자적으로 갱신한다.

    동일 FS 내부에서는 ``os.replace`` 가 POSIX atomic rename 이므로 중간 상태가
    관찰되지 않는다. bind mount 가 **파일 단위** 일 때는 tmp(컨테이너 tmpfs) 와
    target(호스트 FS) 이 다른 mount 를 건너 rename 해야 해 ``EXDEV`` 가 발생한다.
    이 경우 open('w') + fsync 로 직접 overwrite 한다 — 원자성은 약해지지만
    대상 파일은 수 KB 수준이라 write 자체가 짧아 실무상 충분하다. 이 fallback
    은 개별 호출자가 알 필요 없이 내부에서 처리된다.
    """
    target.parent.mkdir(parents=True, exist_ok=True)

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(target.parent),
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp_handle:
            tmp_handle.write(content)
            tmp_handle.flush()
            os.fsync(tmp_handle.fileno())
            tmp_path = Path(tmp_handle.name)

        try:
            os.replace(tmp_path, target)
            tmp_path = None  # replace 성공 — 삭제 대상 없음
            return
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            # bind mount 파일이 다른 device 에 매핑된 경우.
            logger.warning(
                "os.replace EXDEV 감지 — bind mount 파일 단위 감지됨. "
                "직접 overwrite + fsync 로 fallback: target={}",
                target,
            )

        # fallback — 직접 overwrite
        with open(target, "w", encoding="utf-8") as direct_handle:
            direct_handle.write(content)
            direct_handle.flush()
            os.fsync(direct_handle.fileno())

    finally:
        # 위 경로 중 어디에서 예외가 났든 tmp 가 남으면 제거.
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                # 정리 실패는 로깅만 — 메인 예외를 가리지 않도록 무시.
                logger.warning("임시 파일 정리 실패(무시): {}", tmp_path)


def _create_backup(source: Path, backup_root: Path) -> Path:
    """원본 파일을 타임스탬프 이름으로 backup_root 에 복사한다.

    shutil.copy2 로 메타데이터(mtime/권한) 도 보존한다. 백업 루트가 없으면
    생성한다. 반환값은 생성된 백업 파일 경로.
    """
    backup_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(tz=UTC).strftime(BACKUP_FILENAME_PATTERN)
    backup_path = backup_root / f"{timestamp}.yaml"

    # 동일 초 내 재저장 시 충돌 방지 — 접미사 _1, _2 붙임.
    counter = 1
    while backup_path.exists():
        backup_path = backup_root / f"{timestamp}_{counter}.yaml"
        counter += 1
        if counter > 100:
            raise RuntimeError(
                f"백업 파일 이름 충돌이 100 회 이상 발생 — backup_root={backup_root!r}"
            )

    shutil.copy2(source, backup_path)
    logger.info("sources.yaml 백업 생성: {} → {}", source, backup_path)
    return backup_path


# ──────────────────────────────────────────────────────────────
# 저장 (검증 + 백업 + 원자적 쓰기)
# ──────────────────────────────────────────────────────────────


def save_sources_yaml_text(
    text: str,
    *,
    data_dir: Path | None = None,
) -> SaveResult:
    """편집기에서 제출된 텍스트를 검증·백업·원자적 저장한다.

    단계:
        1. ``validate_sources_yaml_text`` — 실패 시 저장하지 않고 예외 전파.
        2. 원본이 존재하면 ``<data_dir>/backups/sources/YYYYMMDD_HHMMSS.yaml``
           로 백업 (shutil.copy2 로 메타데이터 보존). 원본이 없으면 백업 건너뜀.
        3. ``_atomic_write`` — NamedTemporaryFile + os.replace (bind mount
           파일 단위 EXDEV 시 직접 overwrite fallback).

    검증 실패의 경우 파일에 어떤 변경도 가해지지 않는다 (사용자 원문: '실패 시
    에러 + 저장 안 함').

    Args:
        text:      textarea 제출 원문.
        data_dir:  테스트 주입용. None 이면 PROJECT_ROOT/data.

    Returns:
        SaveResult — target_path / backup_path(없으면 None) / byte_count.

    Raises:
        SourcesYamlValidationError: 검증 실패 — 파일 변경 없음.
        OSError:                    백업 또는 쓰기 실패 — 상황에 따라 일부
                                    성공(백업은 생겼지만 쓰기 실패 등) 가능.
                                    호출자는 원본 파일 상태를 log 로 확인.
    """
    # 1. 검증 — 실패 시 바로 raise, 아무것도 쓰지 않는다.
    validate_sources_yaml_text(text)

    target = get_sources_yaml_path()
    backup_root = get_backup_root(data_dir=data_dir)

    # 2. 백업 — 원본이 있을 때만.
    backup_path: Path | None = None
    if target.exists() and target.is_file():
        try:
            backup_path = _create_backup(target, backup_root)
        except OSError as exc:
            # 백업 실패는 '저장 전 백업' 원칙에 따라 그대로 예외로 전파.
            # 절대 쓰기 단계로 넘어가지 않는다 — 원본은 무결하게 유지.
            logger.error(
                "sources.yaml 백업 실패(저장 중단, 원본은 그대로 유지): "
                "source={} backup_root={} ({}: {})",
                target, backup_root, type(exc).__name__, exc,
            )
            raise

    # 3. 원자적 쓰기.
    _atomic_write(target, text)

    # encode 해서 바이트 수 계산 — UI 안내에만 쓰는 값이라 오차 없어도 무방.
    byte_count = len(text.encode("utf-8"))
    logger.info(
        "sources.yaml 저장 완료: target={} bytes={} backup={}",
        target, byte_count, backup_path,
    )
    return SaveResult(
        target_path=target,
        backup_path=backup_path,
        byte_count=byte_count,
    )


__all__ = [
    "BACKUP_FILENAME_PATTERN",
    "BACKUP_SUBPATH",
    "SOURCES_YAML_MOUNT_ENV_VAR",
    "SaveResult",
    "SourcesYamlValidationError",
    "get_backup_root",
    "get_sources_yaml_path",
    "load_sources_yaml_text",
    "save_sources_yaml_text",
    "validate_sources_yaml_text",
]
