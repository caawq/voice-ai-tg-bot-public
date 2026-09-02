"""
Вечерний чек-ин: таймзона-осознанный выбор адресатов (services/checkin.py) и
хендлеры кнопок (bot/handlers/checkin.py) — с настоящей БД (см.
tests/test_bot_voice.py про то же самое решение и про run()/dispose_engine).

Реальный aiogram Bot/CallbackQuery не создаём — хендлерам и send_checkin
нужны только конкретные атрибуты, поэтому используются лёгкие дублёры, как в
test_bot_voice.py.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import delete, select

import services.checkin as checkin_svc
import bot.handlers.checkin as checkin_handlers
from bot.keyboards import CHECKIN_DELETE_PREFIX, CHECKIN_KEEP_PREFIX, CHECKIN_POSTPONE_PREFIX
from db.models import Item, ItemStatus, ItemType, User
from db.session import dispose_engine, session_scope
from services import timeframe


def run(coro):
    """См. tests/test_bot_voice.py::run — сброс движка БД между event loop'ами."""

    async def _wrapped():
        try:
            return await coro
        finally:
            await dispose_engine()

    return asyncio.run(_wrapped())


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_message(self, chat_id: int, text: str, reply_markup=None):
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return SimpleNamespace(message_id=len(self.sent))


class FakeEditableMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.edits: list[str] = []

    async def edit_text(self, text: str, reply_markup=None):
        self.text = text
        self.edits.append(text)


class FakeCallback:
    def __init__(self, *, data: str, telegram_id: int, message: FakeEditableMessage) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=telegram_id)
        self.message = message
        self.answers: list[dict[str, Any]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False):
        self.answers.append({"text": text, "show_alert": show_alert})


@pytest.fixture(autouse=True)
def _clean_db():
    async def _clean():
        async with session_scope() as session:
            await session.execute(delete(Item))
            await session.execute(delete(User))

    run(_clean())
    yield
    run(_clean())


def _make_user(session, telegram_id: int, timezone: str) -> User:
    user = User(telegram_id=telegram_id, timezone=timezone)
    session.add(user)
    return user


# --- services/checkin.py: чистая логика времени --------------------------------


def test_is_checkin_hour_учитывает_пояс_а_не_время_сервера():
    # 17:00 UTC = 20:00 в Москве (UTC+3), но ещё не вечер в Нью-Йорке (UTC-4 летом).
    at_utc = dt.datetime(2026, 8, 31, 17, 0, tzinfo=dt.timezone.utc)
    assert checkin_svc.is_checkin_hour("Europe/Moscow", at_utc) is True
    assert checkin_svc.is_checkin_hour("America/New_York", at_utc) is False


def test_is_checkin_hour_ловит_дробное_смещение_ровно_один_раз_за_сутки():
    """Asia/Kolkata — UTC+5:30, 20:00 там наступает не на границе часа UTC."""
    tz = "Asia/Kolkata"
    hits = [
        checkin_svc.is_checkin_hour(tz, dt.datetime(2026, 8, 31, hour, 0, tzinfo=dt.timezone.utc))
        for hour in range(24)
    ]
    assert sum(hits) == 1, "ровно один часовой прогон из 24 должен считаться вечерним для этого пояса"


def test_due_users_возвращает_только_тех_у_кого_сейчас_вечер():
    async def _setup():
        async with session_scope() as session:
            _make_user(session, 1, "Europe/Moscow")
            _make_user(session, 2, "America/New_York")

    run(_setup())

    at_utc = dt.datetime(2026, 8, 31, 17, 0, tzinfo=dt.timezone.utc)

    async def _check():
        async with session_scope() as session:
            return await checkin_svc.due_users(session, at_utc)

    due = run(_check())
    assert [u.telegram_id for u in due] == [1]


# --- bot/handlers/checkin.py: рассылка ------------------------------------------


def test_рассылка_шлёт_только_тем_у_кого_есть_невыполненные_задачи():
    async def _setup():
        async with session_scope() as session:
            with_tasks = _make_user(session, 10, "Europe/Moscow")
            without_tasks = _make_user(session, 11, "Europe/Moscow")
            await session.flush()
            today = dt.date(2026, 8, 31)
            session.add(Item(user_id=with_tasks.id, type=ItemType.task, title="Написать отчёт", due_date=today))
            # у without_tasks вообще нет задач — чек-ин ему не нужен

    run(_setup())

    bot = FakeBot()
    at_utc = dt.datetime(2026, 8, 31, 17, 0, tzinfo=dt.timezone.utc)  # 20:00 в Москве
    sent = run(checkin_handlers.run_checkin_broadcast(bot, at_utc=at_utc))

    assert sent == 1
    chat_ids = {m["chat_id"] for m in bot.sent}
    assert chat_ids == {10}
    texts = [m["text"] for m in bot.sent]
    assert checkin_handlers.CHECKIN_INTRO in texts
    assert any("Написать отчёт" in t for t in texts)


def test_рассылка_помечает_просроченные_задачи():
    # "Сегодня" берём динамически (services.timeframe.today), а не датой из
    # прошлого прогона теста: _describe_task считает просрочку от реального
    # "сейчас", а не от at_utc рассылки (at_utc только выбирает адресатов).
    tz = "Europe/Moscow"
    real_today = timeframe.today(tz)
    overdue_since = real_today - dt.timedelta(days=3)

    async def _setup():
        async with session_scope() as session:
            user = _make_user(session, 20, tz)
            await session.flush()
            session.add(
                Item(user_id=user.id, type=ItemType.task, title="Оплатить интернет", due_date=overdue_since)
            )

    run(_setup())

    bot = FakeBot()
    at_utc = timeframe.to_utc(dt.datetime.combine(real_today, dt.time(20, 30)), tz)
    run(checkin_handlers.run_checkin_broadcast(bot, at_utc=at_utc))

    task_texts = [m["text"] for m in bot.sent if m["text"] != checkin_handlers.CHECKIN_INTRO]
    assert len(task_texts) == 1
    assert "просрочено на 3 дн." in task_texts[0]


# --- callback-хендлеры: транзакционные обновления -------------------------------


def test_перенести_на_завтра_сдвигает_due_date_по_поясу_пользователя():
    tz = "Europe/Moscow"
    real_today = timeframe.today(tz)

    async def _setup() -> int:
        async with session_scope() as session:
            user = _make_user(session, 30, tz)
            await session.flush()
            item = Item(user_id=user.id, type=ItemType.task, title="Купить корм коту", due_date=real_today)
            session.add(item)
            await session.flush()
            return item.id

    item_id = run(_setup())
    message = FakeEditableMessage("Купить корм коту")
    callback = FakeCallback(data=f"{CHECKIN_POSTPONE_PREFIX}{item_id}", telegram_id=30, message=message)

    run(checkin_handlers.handle_postpone(callback))

    assert "Перенесено на завтра" in message.text

    async def _fetch():
        async with session_scope() as session:
            return (await session.scalars(select(Item).where(Item.id == item_id))).one()

    saved = run(_fetch())
    assert saved.due_date == real_today + dt.timedelta(days=1)
    assert saved.status is ItemStatus.pending


def test_удалить_мягко_удаляет_задачу():
    async def _setup() -> int:
        async with session_scope() as session:
            user = _make_user(session, 31, "Europe/Moscow")
            await session.flush()
            item = Item(user_id=user.id, type=ItemType.task, title="Разобрать балкон", due_date=dt.date(2026, 8, 31))
            session.add(item)
            await session.flush()
            return item.id

    item_id = run(_setup())
    message = FakeEditableMessage("Разобрать балкон")
    callback = FakeCallback(data=f"{CHECKIN_DELETE_PREFIX}{item_id}", telegram_id=31, message=message)

    run(checkin_handlers.handle_delete(callback))
    assert "Удалено" in message.text

    async def _fetch():
        async with session_scope() as session:
            return (await session.scalars(select(Item).where(Item.id == item_id))).one()

    assert run(_fetch()).status is ItemStatus.deleted


def test_оставить_не_трогает_данные():
    async def _setup() -> int:
        async with session_scope() as session:
            user = _make_user(session, 32, "Europe/Moscow")
            await session.flush()
            item = Item(user_id=user.id, type=ItemType.task, title="Позвонить маме", due_date=dt.date(2026, 8, 31))
            session.add(item)
            await session.flush()
            return item.id

    item_id = run(_setup())
    message = FakeEditableMessage("Позвонить маме")
    callback = FakeCallback(data=f"{CHECKIN_KEEP_PREFIX}{item_id}", telegram_id=32, message=message)

    run(checkin_handlers.handle_keep(callback))
    assert "Оставлено" in message.text

    async def _fetch():
        async with session_scope() as session:
            return (await session.scalars(select(Item).where(Item.id == item_id))).one()

    saved = run(_fetch())
    assert saved.status is ItemStatus.pending
    assert saved.due_date == dt.date(2026, 8, 31)


def test_повторное_удаление_не_падает_и_отвечает_алертом():
    async def _setup() -> int:
        async with session_scope() as session:
            user = _make_user(session, 33, "Europe/Moscow")
            await session.flush()
            item = Item(user_id=user.id, type=ItemType.task, title="Помыть машину", due_date=dt.date(2026, 8, 31))
            session.add(item)
            await session.flush()
            return item.id

    item_id = run(_setup())
    message = FakeEditableMessage("Помыть машину")
    callback1 = FakeCallback(data=f"{CHECKIN_DELETE_PREFIX}{item_id}", telegram_id=33, message=message)
    callback2 = FakeCallback(data=f"{CHECKIN_DELETE_PREFIX}{item_id}", telegram_id=33, message=message)

    run(checkin_handlers.handle_delete(callback1))
    run(checkin_handlers.handle_delete(callback2))

    assert callback2.answers[-1]["show_alert"] is True
    assert len(message.edits) == 1, "второе нажатие не должно ещё раз редактировать сообщение"


def test_чужую_задачу_перенести_нельзя():
    async def _setup() -> int:
        async with session_scope() as session:
            owner = _make_user(session, 40, "Europe/Moscow")
            _make_user(session, 41, "Europe/Moscow")  # чужой пользователь
            await session.flush()
            item = Item(user_id=owner.id, type=ItemType.task, title="Чужая задача", due_date=dt.date(2026, 8, 31))
            session.add(item)
            await session.flush()
            return item.id

    item_id = run(_setup())
    message = FakeEditableMessage("Чужая задача")
    callback = FakeCallback(data=f"{CHECKIN_POSTPONE_PREFIX}{item_id}", telegram_id=41, message=message)

    run(checkin_handlers.handle_postpone(callback))

    assert callback.answers[-1]["show_alert"] is True
    assert message.edits == [], "чужой callback не должен менять чужое сообщение"

    async def _fetch():
        async with session_scope() as session:
            return (await session.scalars(select(Item).where(Item.id == item_id))).one()

    assert run(_fetch()).due_date == dt.date(2026, 8, 31), "данные владельца не тронуты"
