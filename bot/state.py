"""
Состояние диалога вокруг разбора одного голосового: FSM для "жду правку" +
хранилище незавершённых попыток подтверждения.

Здесь, а не в services/, потому что это чисто Telegram-специфичная
бухгалтерия — привязка к chat_id/message_id, а не бизнес-логика.

Хранилище в памяти процесса (обычный dict), не Redis и не БД: попытка — это
черновик, который ещё не подтверждён и не должен пережить перезапуск бота
сам по себе. Перезапустили контейнер во время "жду правку" — пользователь
просто присылает голосовое заново, это не потеря данных (в базе ничего не
успело сохраниться), а маленькое неудобство. Если бот когда-нибудь будет
работать не в одном процессе — этот модуль первое, что придётся переделать
(вынести в Redis или БД).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from aiogram.fsm.state import State, StatesGroup

from services.parse_flow import ParseAttempt


class VoiceFlow(StatesGroup):
    """Состояние чата между нажатием [Исправить] и присланной правкой."""

    awaiting_correction = State()


@dataclass
class PendingAttempt:
    """Попытка разбора одной записи + где именно она показана в чате."""

    attempt: ParseAttempt
    chat_id: int
    message_id: int


# attempt_id -> PendingAttempt. Короткий id (не UUID целиком) — чтобы влезать
# в callback_data вместе с префиксом действия (лимит Telegram — 64 байта).
_PENDING: dict[str, PendingAttempt] = {}


def new_attempt_id() -> str:
    """
    Сгенерировать id заранее, до отправки сообщения.

    Порядок в хендлере обратный интуитивному: id нужен, чтобы собрать
    инлайн-клавиатуру (callback_data), а клавиатура нужна, чтобы отправить
    сообщение, а message_id появляется только после отправки. Поэтому id
    и запоминание попытки — два разных шага, а не один вызов "remember".
    """
    return secrets.token_hex(4)  # 8 символов, с запасом уникально для одного чата


def remember(attempt_id: str, attempt: ParseAttempt, *, chat_id: int, message_id: int) -> None:
    """Запомнить попытку под уже сгенерированным id (см. new_attempt_id)."""
    _PENDING[attempt_id] = PendingAttempt(attempt=attempt, chat_id=chat_id, message_id=message_id)


def get(attempt_id: str) -> PendingAttempt | None:
    return _PENDING.get(attempt_id)


def update(attempt_id: str, attempt: ParseAttempt) -> None:
    """Заменить попытку на новую версию (после коррекции), не трогая chat_id/message_id."""
    pending = _PENDING.get(attempt_id)
    if pending is not None:
        pending.attempt = attempt


def forget(attempt_id: str) -> None:
    """Попытка подтверждена или отменена — больше не нужна."""
    _PENDING.pop(attempt_id, None)
