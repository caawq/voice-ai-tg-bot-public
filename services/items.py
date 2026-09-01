"""
Чтение записей: недельная картинка, вечерний чек-ин, просрочка.

Здесь живут оба горячих запроса из схемы и вычисление просрочки. Про Telegram
модуль не знает ничего — на вход сессия и пользователь, на выходе объекты.

Просрочка не хранится в БД (у статуса нет значения "overdue") и считается
только здесь: task + pending + due_date < сегодня по поясу пользователя.
Поэтому она не может разъехаться с реальностью и не требует ночного cron'а.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Item, ItemStatus, ItemType, User
from services import timeframe

VISIBLE = Item.status != ItemStatus.deleted


def is_overdue(item: Item, today: dt.date) -> bool:
    """
    Просрочена ли задача на дату ``today`` (дата берётся по поясу пользователя).

    Единственное место в проекте, где определяется просрочка. Событие никогда
    не бывает просроченным — оно просто прошло; цель тем более.
    """
    return (
        item.type == ItemType.task
        and item.status == ItemStatus.pending
        and item.due_date is not None
        and item.due_date < today
    )


def week_items_stmt(user: User, monday: dt.date) -> Select[tuple[Item]]:
    """
    Горячий запрос №1: всё, что нужно нарисовать на неделе Пн..Вс.

    События отбираются по моменту в UTC (индекс ix_items_user_start_at), задачи
    по дню (ix_items_user_due_date), цели берутся целиком — они идут фоновой
    полосой прогресса через всю неделю и даты не имеют (ix_items_user_goals).
    """
    start_utc, end_utc = timeframe.week_bounds_utc(monday, user.timezone)
    sunday = monday + dt.timedelta(days=6)
    return (
        select(Item)
        .where(
            Item.user_id == user.id,
            VISIBLE,
            or_(
                and_(Item.type == ItemType.event, Item.start_at >= start_utc, Item.start_at < end_utc),
                and_(Item.type == ItemType.task, Item.due_date.between(monday, sunday)),
                Item.type == ItemType.goal,
            ),
        )
        .order_by(Item.due_date.nulls_last(), Item.start_at.nulls_last(), Item.id)
    )


async def week_items(session: AsyncSession, user: User, monday: dt.date) -> list[Item]:
    """Записи недели одним запросом. monday — понедельник нужной недели."""
    return list((await session.scalars(week_items_stmt(user, monday))).all())


def pending_tasks_stmt(user: User, day: dt.date, *, include_overdue: bool) -> Select[tuple[Item]]:
    """
    Горячий запрос №2: невыполненные задачи на день (и, если нужно, хвост просрочки).

    Оба варианта ложатся на частичный индекс ix_items_user_pending_tasks:
    условие по type и status зашито в сам индекс.
    """
    condition = Item.due_date <= day if include_overdue else Item.due_date == day
    return (
        select(Item)
        .where(
            Item.user_id == user.id,
            Item.type == ItemType.task,
            Item.status == ItemStatus.pending,
            condition,
        )
        .order_by(Item.due_date, Item.id)
    )


async def tasks_due_today(session: AsyncSession, user: User) -> list[Item]:
    """Невыполненные задачи ровно на сегодня — по поясу пользователя."""
    day = timeframe.today(user.timezone)
    return list((await session.scalars(pending_tasks_stmt(user, day, include_overdue=False))).all())


async def evening_checkin_tasks(session: AsyncSession, user: User) -> list[Item]:
    """
    То, что бот вечером предлагает разобрать: сегодняшние невыполненные задачи
    вместе с накопившейся просрочкой.

    Возвращается плоским списком от старых к новым — бот сам решает, как это
    показать. Задачи без даты ("когда-нибудь") сюда не попадают: дёргать ими
    человека каждый вечер — ровно то наказание, которого продукт избегает.
    """
    day = timeframe.today(user.timezone)
    return list((await session.scalars(pending_tasks_stmt(user, day, include_overdue=True))).all())


async def overdue_tasks(session: AsyncSession, user: User) -> list[Item]:
    """Только просроченные задачи — тем же индексом, тем же определением."""
    day = timeframe.today(user.timezone)
    stmt = pending_tasks_stmt(user, day, include_overdue=True).where(Item.due_date < day)
    return list((await session.scalars(stmt)).all())
