"""
services/records.py на живой БД: фильтры списка, пагинация, /today и три
действия карточки (готово / перенос / удаление).

Отдельно проверяется то, что легко сломать незаметно: фильтр "просроченные"
в SQL должен давать ровно то же, что items.is_overdue в Python, — иначе
список и карточка начнут расходиться в показаниях.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from sqlalchemy import delete, select

from db.models import Item, ItemStatus, ItemType, User
from db.session import dispose_engine, session_scope
from services import items as items_svc
from services import records as records_svc
from services import timeframe

TZ = "Europe/Moscow"


def run(coro):
    async def _wrapped():
        try:
            return await coro
        finally:
            await dispose_engine()

    return asyncio.run(_wrapped())


@pytest.fixture(autouse=True)
def _clean_db():
    async def _clean():
        async with session_scope() as session:
            await session.execute(delete(Item))
            await session.execute(delete(User))

    run(_clean())
    yield
    run(_clean())


async def _seed() -> tuple[int, dt.date]:
    """Пользователь и набор записей вокруг «сегодня». Возвращает (user_id, today)."""
    async with session_scope() as session:
        user = User(telegram_id=500, timezone=TZ)
        session.add(user)
        await session.flush()
        today = timeframe.today(TZ)

        session.add_all(
            [
                Item(user_id=user.id, type=ItemType.task, title="Сегодняшняя", due_date=today),
                Item(user_id=user.id, type=ItemType.task, title="Просроченная",
                     due_date=today - dt.timedelta(days=2)),
                Item(user_id=user.id, type=ItemType.task, title="Выполненная",
                     due_date=today, status=ItemStatus.done),
                Item(user_id=user.id, type=ItemType.task, title="Когда-нибудь"),
                Item(user_id=user.id, type=ItemType.event, title="Созвон",
                     start_at=timeframe.to_utc(dt.datetime.combine(today, dt.time(15, 0)), TZ)),
                Item(user_id=user.id, type=ItemType.goal, title="Английский", progress_percent=40),
            ]
        )
        return user.id, today


async def _user(session) -> User:
    return (await session.scalars(select(User).where(User.telegram_id == 500))).one()


def test_фильтр_активные_не_включает_просрочку_и_выполненные():
    run(_seed())

    async def _check():
        async with session_scope() as session:
            user = await _user(session)
            items, pages = await records_svc.list_page(
                session, user, ftype="a", fstat="a", today=timeframe.today(TZ), page=0
            )
            return {i.title for i in items}, pages

    titles, pages = run(_check())
    assert titles == {"Сегодняшняя", "Когда-нибудь", "Созвон", "Английский"}
    assert pages == 1


def test_фильтр_просроченные_совпадает_с_is_overdue():
    run(_seed())

    async def _check():
        async with session_scope() as session:
            user = await _user(session)
            today = timeframe.today(TZ)
            filtered, _ = await records_svc.list_page(
                session, user, ftype="a", fstat="o", today=today, page=0
            )
            everything, _ = await records_svc.list_page(
                session, user, ftype="a", fstat="x", today=today, page=0
            )
            return (
                {i.title for i in filtered},
                {i.title for i in everything if items_svc.is_overdue(i, today)},
            )

    from_sql, from_python = run(_check())
    assert from_sql == {"Просроченная"}
    assert from_sql == from_python, "SQL-фильтр и is_overdue обязаны говорить одно и то же"


def test_фильтр_по_типу_и_выполненные():
    run(_seed())

    async def _check():
        async with session_scope() as session:
            user = await _user(session)
            today = timeframe.today(TZ)
            events, _ = await records_svc.list_page(session, user, ftype="e", fstat="x", today=today, page=0)
            done, _ = await records_svc.list_page(session, user, ftype="a", fstat="d", today=today, page=0)
            return {i.title for i in events}, {i.title for i in done}

    events, done = run(_check())
    assert events == {"Созвон"}
    assert done == {"Выполненная"}


def test_пагинация_режет_по_page_size():
    async def _seed_many():
        async with session_scope() as session:
            user = User(telegram_id=500, timezone=TZ)
            session.add(user)
            await session.flush()
            today = timeframe.today(TZ)
            for n in range(records_svc.PAGE_SIZE * 2 + 3):
                session.add(Item(user_id=user.id, type=ItemType.task, title=f"Задача {n}", due_date=today))

    run(_seed_many())

    async def _check():
        async with session_scope() as session:
            user = await _user(session)
            today = timeframe.today(TZ)
            first, pages = await records_svc.list_page(session, user, ftype="a", fstat="a", today=today, page=0)
            last, _ = await records_svc.list_page(session, user, ftype="a", fstat="a", today=today, page=2)
            beyond, _ = await records_svc.list_page(session, user, ftype="a", fstat="a", today=today, page=99)
            return len(first), pages, len(last), [i.title for i in last] == [i.title for i in beyond]

    first_len, pages, last_len, clamped = run(_check())
    assert first_len == records_svc.PAGE_SIZE
    assert pages == 3
    assert last_len == 3
    assert clamped, "страница за пределами должна схлопываться к последней, а не отдавать пусто"


def test_today_берёт_события_дня_задачи_на_сегодня_и_просрочку_без_целей():
    run(_seed())

    async def _check():
        async with session_scope() as session:
            user = await _user(session)
            return {i.title for i in await records_svc.today_records(session, user, timeframe.today(TZ))}

    assert run(_check()) == {"Сегодняшняя", "Просроченная", "Созвон"}


def test_перенос_события_сохраняет_время_дня():
    run(_seed())

    async def _check():
        async with session_scope() as session:
            user = await _user(session)
            today = timeframe.today(TZ)
            event = (await session.scalars(select(Item).where(Item.title == "Созвон"))).one()
            records_svc.postpone(event, today + dt.timedelta(days=1), TZ)
            return timeframe.to_local(event.start_at, TZ)

    moved = run(_check())
    assert moved.date() == timeframe.today(TZ) + dt.timedelta(days=1)
    assert moved.strftime("%H:%M") == "15:00", "перенос двигает день, а не час встречи"


def test_цель_переносить_нельзя():
    run(_seed())

    async def _check():
        async with session_scope() as session:
            goal = (await session.scalars(select(Item).where(Item.title == "Английский"))).one()
            return records_svc.can_postpone(goal), goal

    can, goal = run(_check())
    assert can is False
    with pytest.raises(ValueError):
        records_svc.postpone(goal, dt.date(2026, 9, 3), TZ)


def test_чужую_запись_не_отдаёт():
    run(_seed())

    async def _check():
        async with session_scope() as session:
            item = (await session.scalars(select(Item).where(Item.title == "Сегодняшняя"))).one()
            mine = await records_svc.get_owned(session, item.id, 500)
            alien = await records_svc.get_owned(session, item.id, 999)
            return mine is not None, alien is None

    mine_ok, alien_blocked = run(_check())
    assert mine_ok and alien_blocked


def test_удалённая_запись_исчезает_из_выборок():
    run(_seed())

    async def _check():
        async with session_scope() as session:
            user = await _user(session)
            today = timeframe.today(TZ)
            item = await records_svc.get_owned(session, (
                await session.scalars(select(Item.id).where(Item.title == "Сегодняшняя"))
            ).one(), 500)
            records_svc.soft_delete(item)
            await session.flush()
            items, _ = await records_svc.list_page(session, user, ftype="a", fstat="x", today=today, page=0)
            return {i.title for i in items}

    assert "Сегодняшняя" not in run(_check())


def test_переключение_фильтров_идёт_по_кругу():
    assert records_svc.next_type("a") == "e"
    assert records_svc.next_type("g") == "a"
    assert records_svc.next_status("a") == "d"
    assert records_svc.next_status("x") == "a"
