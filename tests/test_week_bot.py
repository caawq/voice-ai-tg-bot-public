"""
bot/handlers/week.py с настоящей БД (см. tests/test_bot_voice.py про тот же
приём с run()/dispose_engine). Playwright не запускаем: render_week_image
подменяется фейком, который просто создаёт файл — здесь проверяется не
рендер картинки, а то, что бот правильно её отправляет и убирает временный
файл в любом случае, включая ошибку отправки.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import pathlib
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import delete, select

import bot.handlers.week as week_handlers
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
    def __init__(self, *, fail_send: bool = False) -> None:
        self.sent_photos: list[dict[str, Any]] = []
        self._fail_send = fail_send

    async def send_photo(self, chat_id: int, photo, caption=None, reply_markup=None):
        if self._fail_send:
            raise RuntimeError("сеть моргнула")
        self.sent_photos.append(
            {"chat_id": chat_id, "path": photo.path, "caption": caption, "markup": reply_markup}
        )


class FakeMessage:
    def __init__(self, bot: FakeBot, *, chat_id: int, user_id: int) -> None:
        self.bot = bot
        self.chat = SimpleNamespace(id=chat_id)
        self.from_user = SimpleNamespace(id=user_id)
        self.answered: list[str] = []

    async def answer(self, text: str, **kwargs):
        self.answered.append(text)


@pytest.fixture(autouse=True)
def _fake_render(monkeypatch):
    """render_week_image — синхронная функция с Chromium-подпроцессом; в тестах не нужна."""

    def fake_render(data, theme, out_path):
        pathlib.Path(out_path).write_bytes(b"PNG-STUB")

    monkeypatch.setattr(week_handlers, "render_week_image", fake_render)
    yield


@pytest.fixture(autouse=True)
def _clean_db():
    async def _clean():
        async with session_scope() as session:
            await session.execute(delete(Item))
            await session.execute(delete(User))

    run(_clean())
    yield
    run(_clean())


def _make_user(session, telegram_id: int, timezone: str = "Europe/Moscow") -> User:
    user = User(telegram_id=telegram_id, timezone=timezone)
    session.add(user)
    return user


# --- /week ------------------------------------------------------------------------


def test_week_отправляет_фото_и_убирает_временный_файл():
    run(_setup_user_with_task(100))

    bot = FakeBot()
    message = FakeMessage(bot, chat_id=100, user_id=100)
    run(week_handlers.handle_week(message))

    assert len(bot.sent_photos) == 1
    assert bot.sent_photos[0]["chat_id"] == 100
    sent_path = pathlib.Path(bot.sent_photos[0]["path"])
    assert not sent_path.exists(), "временный PNG должен быть удалён после отправки"

    # Промпт 6, п.5: под обложкой — кнопка на каждую запись недели.
    markup = bot.sent_photos[0]["markup"]
    assert markup is not None
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any("Купить корм коту" in label for label in labels)
    assert bot.sent_photos[0]["caption"], "к обложке нужна подпись — её же редактирует карточка"


def test_week_убирает_временный_файл_даже_если_отправка_упала():
    run(_setup_user_with_task(101))

    bot = FakeBot(fail_send=True)
    message = FakeMessage(bot, chat_id=101, user_id=101)

    leaked_paths = []
    orig_render = week_handlers.render_week_image

    def spying_render(data, theme, out_path):
        orig_render(data, theme, out_path)
        leaked_paths.append(out_path)

    week_handlers.render_week_image = spying_render
    try:
        with pytest.raises(RuntimeError):
            run(week_handlers.handle_week(message))
    finally:
        week_handlers.render_week_image = orig_render

    assert leaked_paths, "рендер должен был случиться до попытки отправки"
    assert not pathlib.Path(leaked_paths[0]).exists(), "файл должен быть убран даже при ошибке send_photo"


async def _setup_user_with_task(telegram_id: int) -> None:
    async with session_scope() as session:
        user = _make_user(session, telegram_id)
        await session.flush()
        today = timeframe.today(user.timezone)
        session.add(Item(user_id=user.id, type=ItemType.task, title="Купить корм коту", due_date=today))


# --- /theme -------------------------------------------------------------------------


def test_theme_сохраняет_валидное_значение():
    async def _setup():
        async with session_scope() as session:
            _make_user(session, 200)

    run(_setup())

    bot = FakeBot()
    message = FakeMessage(bot, chat_id=200, user_id=200)
    run(week_handlers.handle_theme(message, SimpleNamespace(args="dark")))

    assert "dark" in message_last(message)

    async def _fetch():
        async with session_scope() as session:
            return (await session.scalars(select(User).where(User.telegram_id == 200))).one()

    assert run(_fetch()).theme == "dark"


def test_theme_без_аргумента_или_с_мусором_не_трогает_данные():
    async def _setup():
        async with session_scope() as session:
            _make_user(session, 201)

    run(_setup())

    bot = FakeBot()
    message = FakeMessage(bot, chat_id=201, user_id=201)
    run(week_handlers.handle_theme(message, SimpleNamespace(args="сепия")))

    assert "light" in message_last(message) or "dark" in message_last(message)

    async def _fetch():
        async with session_scope() as session:
            return (await session.scalars(select(User).where(User.telegram_id == 201))).one()

    assert run(_fetch()).theme == "light", "дефолт не должен был измениться от невалидного значения"


def message_last(message: FakeMessage) -> str:
    return message.answered[-1]


# --- еженедельная рассылка -----------------------------------------------------------


def test_weekly_broadcast_шлёт_только_тем_у_кого_сейчас_понедельник_утро():
    async def _setup():
        async with session_scope() as session:
            due = _make_user(session, 300, "Europe/Moscow")
            not_due = _make_user(session, 301, "America/New_York")
            await session.flush()
            today = timeframe.today("Europe/Moscow")
            session.add(Item(user_id=due.id, type=ItemType.task, title="Задача", due_date=today))
            session.add(Item(user_id=not_due.id, type=ItemType.task, title="Задача", due_date=today))

    run(_setup())

    bot = FakeBot()
    at_utc = dt.datetime(2026, 8, 31, 5, 0, tzinfo=dt.timezone.utc)  # 8:00 пн в Москве
    sent = run(week_handlers.run_weekly_broadcast(bot, at_utc=at_utc))

    assert sent == 1
    assert bot.sent_photos[0]["chat_id"] == 300
