"""소스 어댑터 레지스트리.

소스 유형 ID 에 따라 적절한 어댑터 인스턴스를 반환한다.
순환 의존을 피하기 위해 어댑터 클래스는 lazy import 로 가져온다.
"""

from __future__ import annotations

from app.config import Settings
from app.scraper.base import BaseSourceAdapter
from app.sources.config_schema import SourceConfig
from app.sources.constants import SOURCE_TYPE_IRIS, SOURCE_TYPE_NTIS


def get_adapter(source_config: SourceConfig, settings: Settings) -> BaseSourceAdapter:
    """소스 유형에 맞는 어댑터 인스턴스를 반환한다.

    각 어댑터 클래스는 처음 호출 시에만 import 되어 순환 의존을 방지한다.

    Args:
        source_config: sources.yaml 에서 로드한 소스 설정.
        settings:       전역 애플리케이션 설정.

    Returns:
        해당 소스의 `BaseSourceAdapter` 서브클래스 인스턴스.

    Raises:
        ValueError: 등록되지 않은 소스 유형인 경우.
    """
    if source_config.id == SOURCE_TYPE_IRIS:
        from app.scraper.iris.adapter import IrisSourceAdapter
        return IrisSourceAdapter(source_config, settings)

    if source_config.id == SOURCE_TYPE_NTIS:
        from app.scraper.ntis.adapter import NtisSourceAdapter
        return NtisSourceAdapter(source_config, settings)

    raise ValueError(
        f"등록되지 않은 소스 유형: {source_config.id!r}. "
        f"지원 소스: {SOURCE_TYPE_IRIS}, {SOURCE_TYPE_NTIS}"
    )


__all__ = ["get_adapter"]
