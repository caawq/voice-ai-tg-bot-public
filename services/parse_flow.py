"""
Флоу подтверждения и коррекции разбора.

Зачем он вообще нужен
---------------------
Бот показывает "Записал как задачу: позвонить клиенту, завтра 15:00 — верно?
[Да] [Исправить]". Кнопка [Да] тривиальна. Кнопка [Исправить] без состояния
превращается в кнопку в никуда: пользователь пишет "не в 15, а в 17", бот
получает обычное сообщение и разбирает его как НОВОЕ дело — получается запись
"не в 15 а в 17" вместо исправленной старой.

Поэтому попытка разбора живёт как объект: исходная расшифровка, что из неё
получилось, в каком мы состоянии и что пользователь уже уточнял. Правка
уходит в модель вместе с прошлым разбором (services.voice_parsing.build_messages),
и модель работает с ним как с черновиком, а не с чистым листом.

Состояния
---------
    awaiting_confirmation
        Разобрали уверенно. Показали "Записал: ... [Да] [Исправить]".
    awaiting_explicit_confirmation
        Разобрали, но плохо: низкая уверенность или ответ модели не прошёл
        валидацию. Показываем то же самое, но формулировкой, которая не делает
        вид, что всё хорошо ("Я не уверен, что понял правильно. Записать так?").
        Автоматически не сохраняем НИЧЕГО.
    awaiting_correction
        Нажали [Исправить]. Ждём правку текстом или голосом. Любое следующее
        сообщение пользователя — это правка к прошлой попытке, а не новое дело.
    confirmed
        Пользователь подтвердил. Только отсюда записи уходят в базу.
    discarded
        Пользователь отказался. Ничего не сохраняем.
    failed
        Модель недоступна или правки не помогли. Предлагаем переформулировать
        или бросить — но не сохраняем угадайку.

Переходы:

    start ─────────────► awaiting_confirmation ──[Да]──────────► confirmed
      │                         │
      │                         └──[Исправить]──► awaiting_correction ──► (разбор заново)
      │
      ├────────────────► awaiting_explicit_confirmation ─[Да]──► confirmed
      │                         └──[Исправить]──► awaiting_correction
      │
      └────────────────► failed ──[Исправить]──► awaiting_correction
                                └──[Отмена]────► discarded

Хранение состояния между сообщениями (FSM aiogram, БД, что угодно) — забота
слоя бота. Здесь только чистые переходы: никакого Telegram, никаких сайд-эффектов.
"""

from __future__ import annotations

import datetime as dt
import enum
from dataclasses import dataclass, field, replace

from services.llm import LLMClient
from services.voice_parsing import CONFIDENCE_THRESHOLD, ParsedItem, ParseIssue, ParseResult, parse_transcript

# Сколько раз подряд можно уточнять. Упирается не в технику, а в человека:
# если после трёх правок бот всё ещё не понял, дальше уточнять — издевательство,
# честнее предложить записать иначе.
MAX_CORRECTION_ROUNDS = 3


class FlowState(str, enum.Enum):
    """Состояние одной попытки разбора. Смысл каждого — в docstring модуля."""

    awaiting_confirmation = "awaiting_confirmation"
    awaiting_explicit_confirmation = "awaiting_explicit_confirmation"
    awaiting_correction = "awaiting_correction"
    confirmed = "confirmed"
    discarded = "discarded"
    failed = "failed"


ALLOWED_TRANSITIONS: dict[FlowState, frozenset[FlowState]] = {
    FlowState.awaiting_confirmation: frozenset(
        {FlowState.awaiting_correction, FlowState.confirmed, FlowState.discarded}
    ),
    FlowState.awaiting_explicit_confirmation: frozenset(
        {FlowState.awaiting_correction, FlowState.confirmed, FlowState.discarded}
    ),
    FlowState.awaiting_correction: frozenset(
        {
            FlowState.awaiting_confirmation,
            FlowState.awaiting_explicit_confirmation,
            FlowState.failed,
            FlowState.discarded,
        }
    ),
    FlowState.failed: frozenset({FlowState.awaiting_correction, FlowState.discarded}),
    FlowState.confirmed: frozenset(),
    FlowState.discarded: frozenset(),
}


class IllegalTransition(RuntimeError):
    """Попытка перейти туда, откуда сюда нельзя. Значит, в слое бота баг."""


@dataclass(frozen=True, slots=True)
class ParseAttempt:
    """
    Попытка разбора одного голосового — вместе со всей её историей.

    Иммутабельна: каждый шаг возвращает новый объект. Так исключён случай,
    когда два апдейта Telegram параллельно правят одно состояние.
    """

    transcript: str
    state: FlowState
    items: list[ParsedItem] = field(default_factory=list)
    issue: ParseIssue = ParseIssue.none
    detail: str = ""
    corrections: list[str] = field(default_factory=list)
    voice_file_id: str | None = None

    @property
    def round(self) -> int:
        """Номер попытки: 1 — исходная, дальше по числу правок."""
        return len(self.corrections) + 1

    @property
    def is_terminal(self) -> bool:
        return self.state in (FlowState.confirmed, FlowState.discarded)

    @property
    def saveable_items(self) -> list[ParsedItem]:
        """
        Записи, которые разрешено класть в базу.

        Единственное состояние, из которого что-то сохраняется, — confirmed.
        Ни низкая уверенность, ни битый ответ модели, ни "мы почти уверены"
        сюда не проходят.
        """
        return list(self.items) if self.state is FlowState.confirmed else []


def _state_for(result: ParseResult) -> FlowState:
    """
    В какое состояние переводит результат разбора.

    Всё, что не безупречно, но что-то дало, требует явного подтверждения; всё,
    что не дало ничего, — это failed с объяснением.
    """
    if result.is_clean:
        return FlowState.awaiting_confirmation
    if result.items and result.issue in (ParseIssue.low_confidence, ParseIssue.invalid_model_response):
        return FlowState.awaiting_explicit_confirmation
    # Ни одной записи (или провайдер лёг) — подтверждать нечего.
    return FlowState.failed


def _transition(attempt: ParseAttempt, new_state: FlowState, **changes) -> ParseAttempt:
    allowed = ALLOWED_TRANSITIONS[attempt.state]
    if new_state not in allowed:
        raise IllegalTransition(f"{attempt.state.value} -> {new_state.value} не разрешён")
    return replace(attempt, state=new_state, **changes)


async def start(
    transcript: str,
    *,
    client: LLMClient,
    today: dt.date,
    timezone: str,
    voice_file_id: str | None = None,
) -> ParseAttempt:
    """Первый разбор голосового. Дальше — только через функции этого модуля."""
    result = await parse_transcript(transcript, client=client, today=today, timezone=timezone)
    return ParseAttempt(
        transcript=transcript,
        state=_state_for(result),
        items=list(result.items),
        issue=result.issue,
        detail=result.detail,
        voice_file_id=voice_file_id,
    )


def split_attempts(
    transcript: str,
    items: list[ParsedItem],
    *,
    voice_file_id: str | None = None,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
) -> list[ParseAttempt]:
    """
    Разбить один результат разбора (несколько записей из одного голосового) на
    независимые попытки — по одной на запись.

    Так бот присылает не одно комбинированное подтверждение на всё голосовое,
    а отдельную карточку на каждую запись — и это не просто буквальное
    соответствие макету, а более честный UX: в "напомни позвонить маме и купи
    корм коту" бот может быть уверен в задаче про корм, но не уверен в часе
    звонка. С одним общим подтверждением слабая запись понижала бы
    уверенность всего сообщения; здесь задача подтверждается сразу, а
    сомнительная запись переспрашивается отдельно, независимо от остальных.

    Используется, когда результат уже получен целиком одним вызовом модели
    (bot/handlers/voice.py вызывает services.voice_parsing.parse_transcript
    напрямую, а не через start()) — сюда, в отличие от start(), transcript
    не парсится заново, только раскладывается по попыткам.
    """
    attempts = []
    for item in items:
        clean = item.confidence_score >= confidence_threshold
        attempts.append(
            ParseAttempt(
                transcript=transcript,
                state=FlowState.awaiting_confirmation if clean else FlowState.awaiting_explicit_confirmation,
                items=[item],
                issue=ParseIssue.none if clean else ParseIssue.low_confidence,
                voice_file_id=voice_file_id,
            )
        )
    return attempts


def request_correction(attempt: ParseAttempt) -> ParseAttempt:
    """
    Нажата кнопка [Исправить].

    Сам разбор не трогаем: он нужен как контекст для следующего вызова модели.
    Меняется только состояние — теперь следующее сообщение пользователя читается
    как правка, а не как новое дело.
    """
    return _transition(attempt, FlowState.awaiting_correction)


async def apply_correction(
    attempt: ParseAttempt,
    correction: str,
    *,
    client: LLMClient,
    today: dt.date,
    timezone: str,
) -> ParseAttempt:
    """
    Применить правку пользователя (текстом или расшифровкой голосового).

    Модель получает исходную фразу, прошлый разбор и правку — и возвращает
    полный исправленный список. Это ключевое отличие от "разобрать правку как
    новое сообщение": "не в 15, а в 17" сам по себе никакого дела не содержит.
    """
    if attempt.state is not FlowState.awaiting_correction:
        raise IllegalTransition(f"правку ждут только в awaiting_correction, а не в {attempt.state.value}")

    correction = correction.strip()
    if not correction:
        raise ValueError("пустая правка")

    if attempt.round > MAX_CORRECTION_ROUNDS:
        return _transition(
            attempt,
            FlowState.failed,
            detail=f"после {MAX_CORRECTION_ROUNDS} правок разбор так и не сошёлся",
        )

    result = await parse_transcript(
        attempt.transcript,
        client=client,
        today=today,
        timezone=timezone,
        previous_items=attempt.items,
        correction=correction,
    )
    return _transition(
        attempt,
        _state_for(result),
        items=list(result.items),
        issue=result.issue,
        detail=result.detail,
        corrections=[*attempt.corrections, correction],
    )


def confirm(attempt: ParseAttempt) -> ParseAttempt:
    """Нажата [Да]. Только после этого записи можно сохранять."""
    if not attempt.items:
        raise IllegalTransition("нечего подтверждать: разбор пуст")
    return _transition(attempt, FlowState.confirmed)


def discard(attempt: ParseAttempt) -> ParseAttempt:
    """Пользователь отказался. Ничего не сохраняем."""
    return _transition(attempt, FlowState.discarded)


def confirmation_text(attempt: ParseAttempt) -> str:
    """
    Текст подтверждения — тоже бизнес-логика, поэтому живёт здесь, а не в хендлере.

    Формулировка зависит от состояния: при сомнении бот не изображает
    уверенность, а честно говорит, что мог понять неправильно.
    """
    lines = [describe_item(item) for item in attempt.items]
    body = "\n".join(f"• {line}" for line in lines)

    if attempt.state is FlowState.awaiting_confirmation:
        head = "Записал:" if len(lines) == 1 else "Записал сразу несколько:"
        return f"{head}\n{body}\n\nВерно?"

    if attempt.state is FlowState.awaiting_explicit_confirmation:
        if attempt.issue is ParseIssue.invalid_model_response:
            head = "Я понял с трудом и мог собрать это неправильно."
        else:
            head = "Не уверен, что понял правильно."
        return f"{head} Получилось так:\n{body}\n\nЗаписывать? Если нет — поправьте, я переспрошу."

    if attempt.state is FlowState.failed:
        return (
            "Не смог разобрать это сообщение. Скажите иначе — например, "
            "«завтра в 15 позвонить клиенту» — или напишите текстом."
        )

    return body


def describe_item(item: ParsedItem) -> str:
    """Человеческое описание записи для подтверждения."""
    from db.models import ItemType

    if item.type is ItemType.goal:
        progress = f", прогресс {item.goal_progress}%" if item.goal_progress else ""
        return f"цель: {item.label}{progress}"

    when = ""
    if item.date and item.time:
        when = f", {item.date.strftime('%d.%m')} в {item.time.strftime('%H:%M')}"
    elif item.date:
        when = f", {item.date.strftime('%d.%m')}"
    else:
        when = ", без даты"

    kind = "событие" if item.type is ItemType.event else "задача"
    return f"{kind}: {item.label}{when}"
