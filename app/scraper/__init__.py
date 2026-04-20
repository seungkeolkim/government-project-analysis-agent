"""사업공고 수집 스크래퍼 패키지.

소스별 어댑터(iris/, ntis/ 등)를 포함하며, 외부 코드는
`registry.get_adapter()` 를 통해 어댑터를 획득한다.
"""

from app.scraper.base import BaseSourceAdapter
from app.scraper.registry import get_adapter

__all__ = [
    "BaseSourceAdapter",
    "get_adapter",
]
