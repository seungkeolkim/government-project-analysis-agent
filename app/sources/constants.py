"""공고 수집 소스 유형 상수.

DB의 source_type 컬럼 값으로 사용한다.
Enum 대신 상수로 정의하여 외부 입력(YAML 등)과의 매핑 비용을 줄인다.
"""

SOURCE_TYPE_IRIS: str = "IRIS"
SOURCE_TYPE_NTIS: str = "NTIS"

# 지원하는 소스 유형 전체 목록 (순서: 우선순위 기준)
ALL_SOURCE_TYPES: tuple[str, ...] = (SOURCE_TYPE_IRIS, SOURCE_TYPE_NTIS)

__all__ = [
    "SOURCE_TYPE_IRIS",
    "SOURCE_TYPE_NTIS",
    "ALL_SOURCE_TYPES",
]
