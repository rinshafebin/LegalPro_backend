"""Offset pagination shared by list endpoints."""

import math
from typing import Annotated, Any

from fastapi import Depends, Query
from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession


class PageParams(BaseModel):
    page: int
    page_size: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


def _page_params(
    page: Annotated[int, Query(ge=1, le=10_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> PageParams:
    return PageParams(page=page, page_size=page_size)


PageDep = Annotated[PageParams, Depends(_page_params)]


class Page[T](BaseModel):
    items: list[T]
    total: int
    page: int
    page_size: int
    pages: int


async def paginate(
    session: AsyncSession, stmt: Select[Any], params: PageParams
) -> tuple[list[Any], int]:
    """Run `stmt` for one page. Returns (rows, total). `stmt` must already be ordered."""
    total = await session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    rows = (await session.scalars(stmt.limit(params.page_size).offset(params.offset))).all()
    return list(rows), int(total or 0)


def make_page[T](items: list[T], total: int, params: PageParams) -> Page[T]:
    return Page[T](
        items=items,
        total=total,
        page=params.page,
        page_size=params.page_size,
        pages=math.ceil(total / params.page_size) if total else 0,
    )
