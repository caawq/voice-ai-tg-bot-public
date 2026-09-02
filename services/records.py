"""
Выборки и изменения записей для кнопочного интерфейса (Промпт 6).

Здесь живут фильтры списка (/list), выборка "на сегодня" (/today) и три
действия карточки: готово, перенос, удаление. Про Telegram модуль не знает
ничего — на входе сессия, пользователь и коды фильтров, на выходе объекты и
числа страниц.

Просрочка, как и везде в проекте, отдельным полем не хранится: фильтр
"просроченные" — это тот же самый предикат (task + pending + due_date <
сегодня по поясу пользователя), что и services.items.is_overdue, только
выраженный в SQL, чтобы фильтровать и считать страницы на стороне БД.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Item, ItemStatus, ItemType, User
from services import timeframe

PAGE_SIZE = 8

# Короткие коды фильтров: они уезжают в callback_data кнопок, где каждый байт
# на счету (лимит Telegram — 64 байта на всю строку).
TYPE_FILTERS: dict[str, ItemType | None] = {
    "a": None,
    "e": ItemType.event,
    "t": ItemType.task,
    "g": ItemType.goal,
}
TYPE_LABELS = {"a": "все типы", "e": "события", "t": "задачи", "g": "цели"}
TYPE_ORDER = ["a", "e", "t", "g"]

STATUS_LABELS = {"a": "активные", "d": "выполненные", "o": "просроченные", "x": "все"}
STATUS_ORDER = ["a", "d", "o", "x"]


def next_type(code: str) -> str:
    """Следующий тип по кругу — тап по фильтру переключает его."""
    return TYPE_ORDER[(TYPE_ORDER.index(code) + 1) % len(TYPE_ORDER)]


def next_status(code: str) -> str:
    return STATUS_ORDER[(STATUS_ORDER.index(code) + 1) % len(STATUS_ORDER)]


def _filtered_stmt(user: User, ftype: str, fstat: str, today: dt.date) -> Select[tuple[Item]]:
    conditions = [Item.user_id == user.id, Item.status != ItemStatus.deleted]

    item_type = TYPE_FILTERS.get(ftype)
    if item_type is not None:
        conditions.append(Item.type == item_type)

    if fstat == "a":
        # Активные — незакрытые и не просроченные. Условие по дате отсеивает
        # только задачи: у событий и целей due_date всегда NULL.
        conditions.append(Item.status == ItemStatus.pending)
        conditions.append(or_(Item.due_date.is_(None), Item.due_date >= today))
    elif fstat == "d":
        conditions.append(Item.status == ItemStatus.done)
    elif fstat == "o":
        conditions += [
            Item.type == ItemType.task,
            Item.status == ItemStatus.pending,
            Item.due_date < today,
        ]
    # "x" — всё, кроме удалённых: дополнительных условий нет.

    return select(Item).where(*conditions)


async def list_page(
    session: AsyncSession, user: User, *, ftype: str, fstat: str, today: dt.date, page: int
) -> tuple[list[Item], int]:
    """
    Страница списка и общее число страниц (минимум 1, даже когда записей нет —
    так вызывающему коду не нужно отдельно обрабатывать пустой список).
    """
    stmt = _filtered_stmt(user, ftype, fstat, today)
    total = (await session.scalars(select(func.count()).select_from(stmt.subquery()))).one()
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    ordered = stmt.order_by(Item.due_date.nulls_last(), Item.start_at.nulls_last(), Item.id)
    items = list((await session.scalars(ordered.offset(page * PAGE_SIZE).limit(PAGE_SIZE))).all())
    return items, total_pages


async def today_records(session: AsyncSession, user: User, today: dt.date) -> list[Item]:
    """
    Что показывает /today: события сегодняшнего дня, задачи на сегодня и
    висящая просрочка. Цели сюда не попадают — у них нет дня, они не про
    "что сегодня".
    """
    start_utc, end_utc = timeframe.day_bounds_utc(today, user.timezone)
    stmt = (
        select(Item)
        .where(
            Item.user_id == user.id,
            Item.status == ItemStatus.pending,
            or_(
                (Item.type == ItemType.event) & (Item.start_at >= start_utc) & (Item.start_at < end_utc),
                (Item.type == ItemType.task) & (Item.due_date <= today),
            ),
        )
        .order_by(Item.due_date.nulls_last(), Item.start_at.nulls_last(), Item.id)
    )
    return list((await session.scalars(stmt)).all())


async def get_owned(session: AsyncSession, item_id: int, telegram_id: int) -> Item | None:
    """
    Запись по id — только если она принадлежит этому telegram-пользователю и
    ещё не удалена. Проверка владельца здесь, а не в хендлере, чтобы её нельзя
    было забыть ни в одной из кнопок.
    """
    stmt = (
        select(Item)
        .join(User, Item.user_id == User.id)
        .where(Item.id == item_id, User.telegram_id == telegram_id, Item.status != ItemStatus.deleted)
    )
    return (await session.scalars(stmt)).one_or_none()


def mark_done(item: Item) -> None:
    item.status = ItemStatus.done


def soft_delete(item: Item) -> None:
    """Мягкое удаление — как и везде в проекте: строка остаётся, статус меняется."""
    item.status = ItemStatus.deleted


def can_postpone(item: Item) -> bool:
    """У цели нет даты — переносить нечего (см. CHECK ck_items_goal_shape)."""
    return item.type in (ItemType.event, ItemType.task)


def postpone(item: Item, target: dt.date, timezone: str) -> None:
    """
    Перенести запись на дату ``target`` (по локальному календарю пользователя).

    У события переносится день, а время дня сохраняется: "перенести на завтра"
    для звонка в 15:00 — это завтра в 15:00, а не завтра в тот же момент UTC
    (иначе перенос через смену летнего времени сдвигал бы час встречи).
    """
    if item.type is ItemType.task:
        item.due_date = target
        return
    if item.type is ItemType.event:
        local = timeframe.to_local(item.start_at, timezone)
        item.start_at = timeframe.to_utc(dt.datetime.combine(target, local.time()), timezone)
        return
    raise ValueError("перенос возможен только для события или задачи")
