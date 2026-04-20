"""첨부파일 저장 경로 빌더 및 파일명 정제 유틸.

파일시스템에 안전한 경로를 생성하고, 경로 트래버설을 방어한다.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote


def sanitize_filename(raw_name: str) -> str:
    """다운로드 파일명을 파일시스템 안전한 형태로 정제한다.

    정제 순서:
        1. URL 인코딩 디코딩 (Content-Disposition 헤더 처리)
        2. basename 만 추출 (경로 트래버설 방어)
        3. 금지 문자 치환: / \\ : * ? " < > |  →  _
        4. 공백 → _
        5. 선행 점(.) 제거 (숨김 파일 방지)
        6. 200자 초과 시 확장자 보존하면서 잘라냄
        7. 빈 결과 → 'attachment' 로 치환

    Args:
        raw_name: 원본 파일명 (URL 인코딩 포함 가능).

    Returns:
        정제된 파일명 문자열.
    """
    # 1. URL 인코딩 디코딩
    decoded = unquote(raw_name)

    # 2. basename 만 추출 — Path().name 이 경로 트래버설 방어
    name = Path(decoded).name

    # 3. 금지 문자 치환
    name = re.sub(r'[/\\:*?"<>|]', "_", name)

    # 4. 공백 → _
    name = name.replace(" ", "_")

    # 5. 선행 점 제거
    name = name.lstrip(".")

    # 6. 200자 초과 시 확장자 보존하면서 잘라냄
    if len(name) > 200:
        stem = Path(name).stem
        ext = Path(name).suffix
        name = stem[: 200 - len(ext)] + ext

    # 7. 빈 결과 방어
    return name or "attachment"


def sanitize_path_component(raw: str) -> str:
    """소스 타입·공고 ID 등 경로 조각을 파일시스템 안전한 형태로 정제한다.

    파일명보다 규칙이 단순하다: 금지 문자를 `_`로 치환하고 경로 트래버설(`..`)을 제거한다.
    IRIS는 숫자 ID이지만 NTIS 등은 임의 문자열이 올 수 있어 sanitize가 필요하다.

    Args:
        raw: 정제 전 경로 조각 문자열.

    Returns:
        정제된 경로 조각. 빈 결과면 '_'를 반환한다.
    """
    # 금지 문자 치환 (Path 구분자, Shell 특수문자 포함)
    cleaned = re.sub(r'[/\\:*?"<>|]', "_", raw)

    # 공백 → _
    cleaned = cleaned.replace(" ", "_")

    # .. (경로 트래버설) 제거
    cleaned = cleaned.replace("..", "_")

    # 선행/후행 점·공백 제거
    cleaned = cleaned.strip(". ")

    return cleaned or "_"


def build_attachment_dir(
    download_dir: Path,
    source_type: str,
    source_announcement_id: str,
) -> Path:
    """첨부파일을 저장할 공고별 디렉터리 경로를 반환한다.

    경로 구조: {download_dir}/{source_type}/{sanitized_source_announcement_id}/

    Args:
        download_dir:            첨부파일 다운로드 루트 디렉터리 (Settings.download_dir).
        source_type:             소스 유형 문자열 (예: 'IRIS', 'NTIS').
        source_announcement_id:  소스가 부여한 공고 고유 ID.

    Returns:
        정제된 경로 조각으로 구성된 디렉터리 `Path` 객체.
        디렉터리 실제 생성은 호출자 책임이다.
    """
    safe_source = sanitize_path_component(source_type)
    safe_ann_id = sanitize_path_component(source_announcement_id)
    return download_dir / safe_source / safe_ann_id


def build_attachment_path(
    download_dir: Path,
    source_type: str,
    source_announcement_id: str,
    original_filename: str,
) -> Path:
    """첨부파일 한 건의 저장 경로를 반환한다.

    경로 구조:
        {download_dir}/{source_type}/{sanitized_source_announcement_id}/{sanitized_filename}

    Args:
        download_dir:            첨부파일 다운로드 루트 디렉터리 (Settings.download_dir).
        source_type:             소스 유형 문자열 (예: 'IRIS', 'NTIS').
        source_announcement_id:  소스가 부여한 공고 고유 ID.
        original_filename:       원본 파일명 (URL 인코딩 포함 가능).

    Returns:
        정제된 경로로 구성된 파일 `Path` 객체.
        상위 디렉터리 생성은 호출자 책임이다.
    """
    ann_dir = build_attachment_dir(download_dir, source_type, source_announcement_id)
    safe_filename = sanitize_filename(original_filename)
    return ann_dir / safe_filename


__all__ = [
    "sanitize_filename",
    "sanitize_path_component",
    "build_attachment_dir",
    "build_attachment_path",
]
