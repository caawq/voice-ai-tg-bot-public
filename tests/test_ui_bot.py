"""
Кнопочный интерфейс (Промпт 6) на живой БД: список, карточка, действия,
настройки.

Главное, что здесь проверяется помимо самих действий, — что экраны
РЕДАКТИРУЮТ сообщение, а не плодят новые (требование п.7), и что под
обложкой недели редактируется подпись, а не текст (у фото текста нет —
Telegram такой вызов отклонит).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import delete, select

import bot.handlers.records as records_handlers
import bot.handlers.settings as settings_handlers
from bot.callbacks import SRC_LIST, SRC_WEEK, ListCB, RecordCB, SettingsCB
from db.models import Item, ItemStatus, ItemType, User
from db.session import dispose_engine, session_scope
from services import timeframe

TZ = "Europe/Moscow"
TG_ID = 700


def run(coro):
    async def _wrapped():
        try:
            return await coro
        finally:
            await dispose_engine()

    return asyncio.run(_wrapped())


class FakeMessage:
    """Сообщение бота: умеет то же, что нужно экранам, и записывает все вызовы."""

    def __init__(self, *, text: str = "", photo: bool = False) -> None:
        self.text = text
        self.photo = [SimpleNamespace(file_id="x")] if photo else None
        self.chat = SimpleNamespace(id=TG_ID)
        self.from_user = SimpleNamespace(id=TG_ID)
        self.edits: list[dict[str, Any]] = []
        self.captions: list[dict[str, Any]] = []
        self.answers: list[dict[str, Any]] = []

    async def edit_text(self, text: str, reply_markup=None):
        self.text = text
        self.edits.append({"text": text, "markup": reply_markup})

    async def edit_caption(self, caption: str, reply_markup=None):
        self.text = caption
        self.captions.append({"text": caption, "markup": reply_markup})

    async def answer(self, text: str, reply_markup=None):
        self.answers.append({"text": text, "markup": reply_markup})
        return FakeMessage(text=text)


class FakeCallback:
    def __init__(self, message: FakeMessage, *, telegram_id: int = TG_ID) -> None:
        self.message = message
        self.from_user = SimpleNamespace(id=telegram_id)
        self.replies: list[dict[str, Any]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False):
        self.replies.append({"text": text, "show_alert": show_alert})


@pytest.fixture(autouse=True)
def _clean_db():
    async def _clean():
        async with session_scope() as session:
            await session.execute(delete(Item))
            await session.execute(delete(User))

    run(_clean())
    yield
    run(_clean())


def seed() -> tuple[int, dt.date]:
    async def _seed():
        async with session_scope() as session:
            user = User(telegram_id=TG_ID, timezone=TZ)
            session.add(user)
            await session.flush()
            today = timeframe.today(TZ)
            task = Item(user_id=user.id, type=ItemType.task, title="Купить корм коту", due_date=today)
            session.add(task)
            session.add(Item(user_id=user.id, type=ItemType.event, title="Созвон",
                             start_at=timeframe.to_utc(dt.datetime.combine(today, dt.time(15, 0)), TZ)))
            await session.flush()
            return task.id, today

    return run(_seed())


def fetch_item(item_id: int) -> Item:
    async def _fetch():
        async with session_scope() as session:
            return (await session.scalars(select(Item).where(Item.id == item_id))).one()

    return run(_fetch())


def rec_cb(item_id: int, act: str, *, src: str = SRC_LIST, arg: str = "") -> RecordCB:
    return RecordCB(act=act, id=item_id, src=src, page=0, ftype="a", fstat="a", arg=arg)


# --- список -----------------------------------------------------------------------


def test_list_шлёт_одно_сообщение_с_кнопками_записей_и_фильтрами():
    seed()
    message = FakeMessage()
    run(records_handlers.handle_list(message))

    assert len(message.answers) == 1
    markup = message.answers[0]["markup"]
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any("Купить корм коту" in label for label in labels)
    assert any(label.startswith("Тип:") for label in labels)
    assert any(label.startswith("Статус:") for label in labels)


def test_переключение_фильтра_редактирует_то_же_сообщение():
    seed()
    message = FakeMessage(text="Все записи")
    callback = FakeCallback(message)
    data = ListCB(act="ft", src=SRC_LIST, page=0, ftype="a", fstat="a")

    run(records_handlers.handle_list_callback(callback, data))

    assert message.answers == [], "новых сообщений быть не должно"
    assert len(message.edits) == 1
    assert "события" in message.edits[0]["text"], "фильтр типа переключился на следующий по кругу"


# --- карточка ---------------------------------------------------------------------


def test_тап_по_записи_открывает_карточку_в_том_же_сообщении():
    item_id, _ = seed()
    message = FakeMessage(text="Все записи")
    callback = FakeCallback(message)

    run(records_handlers.handle_open(callback, rec_cb(item_id, "o")))

    assert message.answers == []
    assert "Купить корм коту" in message.edits[-1]["text"]
    labels = [b.text for row in message.edits[-1]["markup"].inline_keyboard for b in row]
    assert "✅ Готово" in labels and "🗑 Удалить" in labels and "◀️ Назад к списку" in labels


def test_готово_переводит_запись_в_выполненные():
    item_id, _ = seed()
    callback = FakeCallback(FakeMessage())

    run(records_handlers.handle_done(callback, rec_cb(item_id, "d")))

    assert fetch_item(item_id).status is ItemStatus.done
    assert "Статус: выполнено" in callback.message.edits[-1]["text"]


def test_перенос_на_завтра_двигает_дату():
    item_id, today = seed()
    callback = FakeCallback(FakeMessage())

    run(records_handlers.handle_postpone(callback, rec_cb(item_id, "p", arg="m")))

    assert fetch_item(item_id).due_date == today + dt.timedelta(days=1)
    assert "завтра" in callback.replies[-1]["text"]


def test_удаление_идёт_через_подтверждение():
    item_id, _ = seed()
    message = FakeMessage()
    callback = FakeCallback(message)

    run(records_handlers.handle_delete_ask(callback, rec_cb(item_id, "da")))
    assert "Удалить запись" in message.edits[-1]["text"]
    assert fetch_item(item_id).status is ItemStatus.pending, "до подтверждения ничего не удаляем"

    run(records_handlers.handle_delete(FakeCallback(message), rec_cb(item_id, "dy")))
    assert fetch_item(item_id).status is ItemStatus.deleted
    assert message.answers == [], "после удаления возвращаемся в список тем же сообщением"


def test_действие_над_чужой_записью_отклоняется():
    item_id, _ = seed()
    callback = FakeCallback(FakeMessage(), telegram_id=999)

    run(records_handlers.handle_done(callback, rec_cb(item_id, "d")))

    assert callback.replies[-1]["show_alert"] is True
    assert fetch_item(item_id).status is ItemStatus.pending


# --- обложка недели ----------------------------------------------------------------


def test_под_обложкой_недели_редактируется_подпись_а_не_текст():
    item_id, _ = seed()
    photo_message = FakeMessage(photo=True)
    callback = FakeCallback(photo_message)

    run(records_handlers.handle_open(callback, rec_cb(item_id, "o", src=SRC_WEEK)))

    assert photo_message.edits == [], "у сообщения с фото нет текста — edit_text недопустим"
    assert "Купить корм коту" in photo_message.captions[-1]["text"]

    # и «Назад» возвращает список недели туда же, в подпись
    run(records_handlers.handle_back(FakeCallback(photo_message), rec_cb(item_id, "b", src=SRC_WEEK)))
    assert len(photo_message.captions) == 2
    labels = [b.text for row in photo_message.captions[-1]["markup"].inline_keyboard for b in row]
    assert any("Купить корм коту" in label for label in labels)


# --- настройки ----------------------------------------------------------------------


def test_настройки_меняют_час_чек_ина():
    seed()
    message = FakeMessage()
    run(settings_handlers.handle_settings(message))
    assert "20:00" in message.answers[0]["text"], "по умолчанию — прежние 20:00"

    callback = FakeCallback(FakeMessage())
    run(settings_handlers.handle_set_hour(callback, SettingsCB(act="h", val=21)))

    async def _fetch():
        async with session_scope() as session:
            return (await session.scalars(select(User).where(User.telegram_id == TG_ID))).one()

    assert run(_fetch()).checkin_hour == 21
    assert "21:00" in callback.message.edits[-1]["text"]
