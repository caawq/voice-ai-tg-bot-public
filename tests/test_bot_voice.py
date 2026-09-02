"""
Интеграционная проверка bot/handlers/voice.py: от «пришло голосовое» до
записи в базе и до флоу коррекции — с фейковыми LLM/транскрипцией (как в
остальных тестах) и настоящей БД (см. tests/conftest.py DATABASE_URL из .env
тестового окружения). ffmpeg не нужен и не запускается: ogg_to_wav
подменяется фейком, тестируется не конвертация звука (у неё нет бизнес-логики),
а связка хендлеров, флоу подтверждения/коррекции и слоя данных.

Реальные aiogram Message/CallbackQuery не создаём — хендлерам нужны только
конкретные атрибуты (bot, voice.file_id, from_user.id, chat.id, answer(...)),
поэтому используются лёгкие дублёры. FSMContext — настоящий (MemoryStorage),
чтобы проверить именно то, как хендлеры её используют.
"""

from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey

import bot.handlers.voice as voice_handlers
from bot.keyboards import CONFIRM_PREFIX, CORRECT_PREFIX
from bot.state import VoiceFlow
from bot.state import get as get_pending
from conftest import TIMEZONE, TODAY, FakeLLM, item, payload
from db.models import Item
from db.session import dispose_engine, session_scope
from sqlalchemy import delete

from db.models import User


def run(coro):
    """
    Каждый asyncio.run() — новый event loop, а движок БД (db/session.py) —
    синглтон на процесс, привязанный к loop, в котором создан. Без сброса
    между вызовами второй run() падает на "attached to a different loop".
    В самом боте это не проблема (один процесс — один вечный loop polling'а),
    это чисто особенность теста, дёргающего run() по многу раз за тест.
    """

    async def _wrapped():
        try:
            return await coro
        finally:
            await dispose_engine()

    return asyncio.run(_wrapped())


class FakeTranscription:
    """Как FakeLLM (tests/conftest.py), только для транскрипции: очередь текстов."""

    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)
        self.calls: list[dict[str, Any]] = []

    async def transcribe(self, *, audio: bytes, mime_type: str) -> str:
        self.calls.append({"audio": audio, "mime_type": mime_type})
        if not self.texts:
            raise AssertionError("FakeTranscription позвали больше раз, чем заготовлено ответов")
        return self.texts.pop(0)


class FakeBot:
    """Записывает всё, что хендлеры пытаются сделать через bot.*, вместо сети."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.edited: list[dict[str, Any]] = []
        self._next_message_id = 1000

    async def download(self, file_id: str) -> io.BytesIO:
        return io.BytesIO(b"raw-ogg-bytes")

    async def send_message(self, chat_id: int, text: str, reply_markup=None):
        self._next_message_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return SimpleNamespace(message_id=self._next_message_id)

    async def edit_message_text(self, text: str, *, chat_id: int, message_id: int, reply_markup=None):
        self.edited.append(
            {"chat_id": chat_id, "message_id": message_id, "text": text, "reply_markup": reply_markup}
        )


class FakeMessage:
    def __init__(self, bot: FakeBot, *, chat_id: int, user_id: int, voice_file_id: str | None = None,
                 text: str | None = None) -> None:
        self.bot = bot
        self.chat = SimpleNamespace(id=chat_id)
        self.from_user = SimpleNamespace(id=user_id)
        self.voice = SimpleNamespace(file_id=voice_file_id) if voice_file_id else None
        self.text = text
        self.answered: list[dict[str, Any]] = []

    async def answer(self, text: str, reply_markup=None):
        self._record(text, reply_markup)
        return SimpleNamespace(message_id=1)

    def _record(self, text, reply_markup):
        self.answered.append({"text": text, "reply_markup": reply_markup})
        self.bot.sent.append({"chat_id": self.chat.id, "text": text, "reply_markup": reply_markup})


class FakeEditableMessage:
    """message внутри CallbackQuery — то, что редактирует [Да]/[Исправить]."""

    def __init__(self, bot: FakeBot, *, chat_id: int, message_id: int, text: str) -> None:
        self.bot = bot
        self.chat = SimpleNamespace(id=chat_id)
        self.message_id = message_id
        self.text = text
        self.edits: list[dict[str, Any]] = []

    async def edit_text(self, text: str, reply_markup=None):
        self.text = text
        self.edits.append({"text": text, "reply_markup": reply_markup})


class FakeCallback:
    def __init__(self, *, data: str, user_id: int, message: FakeEditableMessage) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = message
        self.answers: list[dict[str, Any]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False):
        self.answers.append({"text": text, "show_alert": show_alert})


@pytest.fixture(autouse=True)
def _no_real_ffmpeg(monkeypatch):
    """ogg_to_wav не тестируем здесь — подменяем, чтобы не требовать ffmpeg в песочнице."""

    async def fake_ogg_to_wav(raw: bytes) -> bytes:
        return b"fake-wav"

    monkeypatch.setattr(voice_handlers, "ogg_to_wav", fake_ogg_to_wav)
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


def _fsm(chat_id: int, user_id: int) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=chat_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


TOMORROW = (TODAY + __import__("datetime").timedelta(days=1)).isoformat()


def test_голосовое_с_двумя_записями_шлёт_две_карточки_по_очереди():
    """Промпт 3, п.1 и п.4: на несколько дел в одном голосовом — отдельная карточка на каждое."""
    llm = FakeLLM(
        payload(
            item("task", "купить корм коту", date=TOMORROW),
            item("event", "позвонить клиенту", date=TOMORROW, time="15:00"),
        )
    )
    transcription = FakeTranscription("напомни купить корм коту и завтра в 15 позвонить клиенту")
    bot = FakeBot()
    message = FakeMessage(bot, chat_id=1, user_id=777, voice_file_id="AwACfile1")

    run(voice_handlers.handle_voice(message, llm_client=llm, transcription_client=transcription))

    assert len(bot.sent) == 2, "два дела -> два отдельных сообщения, а не одно на оба"
    assert "корм коту" in bot.sent[0]["text"]
    assert "позвонить клиенту" in bot.sent[1]["text"]
    for sent in bot.sent:
        assert sent["reply_markup"] is not None, "у каждой карточки своя клавиатура"


def test_да_сохраняет_запись_в_базу_с_voice_file_id_и_транскриптом():
    """Промпт 3, п.2."""
    llm = FakeLLM(payload(item("task", "купить корм коту", date=TOMORROW)))
    transcription = FakeTranscription("напомни купить корм коту")
    bot = FakeBot()
    message = FakeMessage(bot, chat_id=1, user_id=777, voice_file_id="AwACfile2")

    run(voice_handlers.handle_voice(message, llm_client=llm, transcription_client=transcription))
    assert len(bot.sent) == 1
    reply_markup = bot.sent[0]["reply_markup"]
    attempt_id = reply_markup.inline_keyboard[0][0].callback_data[len(CONFIRM_PREFIX):]

    editable = FakeEditableMessage(bot, chat_id=1, message_id=bot._next_message_id, text=bot.sent[0]["text"])
    callback = FakeCallback(data=f"{CONFIRM_PREFIX}{attempt_id}", user_id=777, message=editable)

    run(voice_handlers.handle_confirm(callback))

    assert "Сохранено" in editable.text
    assert get_pending(attempt_id) is None, "подтверждённая попытка забыта"

    async def _fetch():
        async with session_scope() as session:
            from sqlalchemy import select
            rows = (await session.scalars(select(Item))).all()
            return rows

    rows = run(_fetch())
    assert len(rows) == 1
    saved = rows[0]
    assert saved.title == "купить корм коту"
    assert saved.voice_file_id == "AwACfile2"
    assert saved.source_transcript == "напомни купить корм коту"


def test_исправить_ждёт_правку_и_применяет_её_к_той_же_карточке():
    """Промпт 3, п.3: [Исправить] не создаёт новое дело, а правит старое."""
    llm = FakeLLM(
        payload(item("event", "позвонить клиенту", date=TOMORROW, time="15:00")),
        payload(item("event", "позвонить клиенту", date=TOMORROW, time="17:00")),
    )
    transcription = FakeTranscription("напомни завтра в 15 позвонить клиенту")
    bot = FakeBot()
    message = FakeMessage(bot, chat_id=5, user_id=42, voice_file_id="AwACfile3")

    run(voice_handlers.handle_voice(message, llm_client=llm, transcription_client=transcription))
    reply_markup = bot.sent[0]["reply_markup"]
    attempt_id = reply_markup.inline_keyboard[0][1].callback_data[len(CORRECT_PREFIX):]

    editable = FakeEditableMessage(bot, chat_id=5, message_id=999, text=bot.sent[0]["text"])
    callback = FakeCallback(data=f"{CORRECT_PREFIX}{attempt_id}", user_id=42, message=editable)
    fsm = _fsm(chat_id=5, user_id=42)

    run(voice_handlers.handle_correct_request(callback, fsm))
    assert run(fsm.get_state()) == VoiceFlow.awaiting_correction.state
    assert "правку" in editable.text.lower()

    correction_message = FakeMessage(bot, chat_id=5, user_id=42, text="не в 15, а в 17")
    run(voice_handlers.handle_correction_text(correction_message, fsm, llm))

    assert run(fsm.get_state()) is None, "FSM-состояние снято после применения правки"
    assert any("17:00" in e["text"] for e in bot.edited), "старую карточку отредактировали, а не прислали новую"
    pending = get_pending(attempt_id)
    assert pending is not None
    assert pending.attempt.items[0].time.strftime("%H:%M") == "17:00"


def test_ничего_не_разобрано_отвечает_текстом_и_не_шлёт_кнопки():
    llm = FakeLLM(payload())
    transcription = FakeTranscription("бла бла непонятно что")
    bot = FakeBot()
    message = FakeMessage(bot, chat_id=1, user_id=777, voice_file_id="AwACfile4")

    run(voice_handlers.handle_voice(message, llm_client=llm, transcription_client=transcription))

    assert len(bot.sent) == 1
    assert bot.sent[0]["reply_markup"] is None
    assert "Не смог разобрать" in bot.sent[0]["text"]


def test_повторное_да_на_ту_же_карточку_не_падает_и_не_дублирует_запись():
    """Двойное нажатие [Да] (двойной тап / повтор апдейта Telegram) — не должно писать в базу дважды."""
    llm = FakeLLM(payload(item("task", "купить корм коту", date=TOMORROW)))
    transcription = FakeTranscription("напомни купить корм коту")
    bot = FakeBot()
    message = FakeMessage(bot, chat_id=1, user_id=777, voice_file_id="AwACfile5")

    run(voice_handlers.handle_voice(message, llm_client=llm, transcription_client=transcription))
    reply_markup = bot.sent[0]["reply_markup"]
    attempt_id = reply_markup.inline_keyboard[0][0].callback_data[len(CONFIRM_PREFIX):]

    editable = FakeEditableMessage(bot, chat_id=1, message_id=1, text=bot.sent[0]["text"])
    callback1 = FakeCallback(data=f"{CONFIRM_PREFIX}{attempt_id}", user_id=777, message=editable)
    callback2 = FakeCallback(data=f"{CONFIRM_PREFIX}{attempt_id}", user_id=777, message=editable)

    run(voice_handlers.handle_confirm(callback1))
    run(voice_handlers.handle_confirm(callback2))

    assert callback2.answers[-1]["show_alert"] is True

    async def _fetch():
        async with session_scope() as session:
            from sqlalchemy import select
            return (await session.scalars(select(Item))).all()

    rows = run(_fetch())
    assert len(rows) == 1, "повторное подтверждение не должно создавать вторую запись"
