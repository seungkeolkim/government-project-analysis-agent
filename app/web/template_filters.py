"""Jinja2 공용 KST 표시 필터 등록 헬퍼 (task 00040-3).

배경:
    프로젝트에는 ``Jinja2Templates`` 인스턴스가 라우터 모듈별로 분리되어 있다
    (``app.web.main`` / ``app.web.routes.admin`` / ``app.auth.routes``). 사용자
    원문의 "Jinja2: app.web.main 에 필터 등록(kst_format, kst_date). 모든
    timestamp 표시는 필터 경유" 요건을 만족시키려면 이 세 인스턴스 모두에
    같은 필터를 달아야 한다 — 어느 한 곳이라도 누락되면 사용자 원문 주의사항
    "일부만 KST/일부 UTC 일관성 깨짐" 이 그대로 재현되기 때문이다.

설계:
    - 본 모듈은 :func:`register_kst_filters` 한 함수만 노출한다. 호출 측은
      Jinja2Templates 가 만들어진 **직후** 한 번만 호출하면 된다 — 같은 dict
      키에 다시 대입하므로 여러 번 호출해도 idempotent.
    - 필터 본체는 ``app.timezone.format_kst`` 한 곳에서 가져온다. 표시 결과의
      단일 진실은 그 모듈이며, 본 모듈은 Jinja 에 노출하는 어댑터일 뿐이다.

노출 필터:
    - ``kst_format(value, fmt=\"%Y-%m-%d %H:%M\")`` — 분 단위가 기본이지만
      ``{{ dt | kst_format(\"%Y-%m-%d %H:%M:%S\") }}`` 처럼 fmt 인자 전달 가능.
    - ``kst_date(value)`` — 일자 (``%Y-%m-%d``) 만 표시.

None 처리:
    두 필터 모두 ``format_kst`` 의 None→"" 정책을 따른다. 템플릿에서 ``or '—'``
    같은 fallback 분기를 호출 측이 결정한다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi.templating import Jinja2Templates

from app.timezone import format_kst


def _kst_date_filter(value: datetime | None) -> str:
    """``{{ dt | kst_date }}`` — KST 기준 일자(``YYYY-MM-DD``) 문자열.

    ``format_kst`` 를 ``%Y-%m-%d`` 포맷으로 호출하는 얇은 어댑터다. 필터로
    노출하기 위해 별도 함수 객체가 필요해 람다 대신 명시적 def 로 둔다 —
    Jinja 디버깅 시 traceback 에 함수 이름이 떠야 누락된 호출자를 찾기 쉽다.
    """
    return format_kst(value, "%Y-%m-%d")


def register_kst_filters(templates: Jinja2Templates) -> None:
    """주어진 ``Jinja2Templates`` 의 환경에 KST 표시 필터를 등록한다.

    호출 위치 (현재 시점):
        - ``app.web.main.create_app`` — 목록/상세/즐겨찾기/index 템플릿.
        - ``app.web.routes.admin`` — 관리자 [수집 제어]/[sources.yaml]/
          [스케줄] 템플릿.
        - ``app.auth.routes`` — 로그인/회원가입 (현재는 datetime 표시 없음.
          향후 회원가입 시각 등 추가될 가능성 대비해 함께 등록한다).

    동작:
        - ``templates.env.filters`` 는 ``dict`` 이므로 같은 키에 다시 대입해도
          예외 없이 덮어쓰기. 같은 인스턴스에 여러 번 호출해도 안전하다.
        - 필터 본체는 모듈 수준 함수 참조이므로 호출 시점에 추가 import 비용
          이 없다.

    Args:
        templates: 필터를 등록할 ``fastapi.templating.Jinja2Templates`` 인스턴스.
    """
    env_filters: dict[str, Any] = templates.env.filters
    env_filters["kst_format"] = format_kst
    env_filters["kst_date"] = _kst_date_filter


__all__ = ["register_kst_filters"]
