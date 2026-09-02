"""
Запись и чтение записей: сохранение из разбора голосового, недельная картинка,
вечерний чек-ин, просрочка.

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
from services.voice_parsing import ParsedItem

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


def item_from_parsed(user: User, parsed: ParsedItem, *, source_transcript: str, voice_file_id: str | None) -> Item:
    """
    Собрать строку items из результата разбора (services/voice_parsing.py).

    Ничего не решает заново — ParsedItem уже прошёл валидацию формы в
    voice_parsing._validate (событию есть час, у цели нет даты и т.д.), эта
    функция только раскладывает уже проверенные поля по колонкам конкретной
    записи и добавляет то, чего нет в ParsedItem: пользователя и контекст
    исходного голосового (принцип "контекст сохраняется" из концепта — по
    ним можно будет переслушать оригинал).
    """
    start_at = None
    if parsed.type is ItemType.event:
        assert parsed.date is not None and parsed.time is not None  # гарантировано валидацией
        local_dt = dt.datetime.combine(parsed.date, parsed.time)
        start_at = timeframe.to_utc(local_dt, user.timezone)

    return Item(
        user_id=user.id,
        type=parsed.type,
        status=ItemStatus.pending,
        title=parsed.label,
        start_at=start_at,
        due_date=parsed.date if parsed.type is ItemType.task else None,
        progress_percent=parsed.goal_progress,
        source_transcript=source_transcript,
        voice_file_id=voice_file_id,
    )


async def save_parsed_item(
    session: AsyncSession,
    user: User,
    parsed: ParsedItem,
    *,
    source_transcript: str,
    voice_file_id: str | None,
) -> Item:
    """
    Сохранить одну подтверждённую запись. Коммит — забота вызывающего кода
    (см. db.session.session_scope), здесь только add + flush, чтобы у объекта
    сразу был id.
    """
    item = item_from_parsed(user, parsed, source_transcript=source_transcript, voice_file_id=voice_file_id)
    session.add(item)
    await session.flush()
    return item
