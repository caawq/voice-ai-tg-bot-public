"""
Приём голосовых: от файла из Telegram до подтверждённой записи в базе.

Склеивает вместе то, что до этого хендлера было чистой, не знающей про
Telegram логикой: services/audio.py (OGG -> WAV), клиент транскрипции и
разбора (services/llm.py), services/voice_parsing.py и services/parse_flow.py
(флоу подтверждения/коррекции), services/items.py (сохранение). Здесь —
только Telegram-специфичная бухгалтерия: откуда взять файл, что показать в
чате, куда положить attempt_id.

Порядок регистрации хендлеров в router важен: хендлеры, ограниченные
состоянием VoiceFlow.awaiting_correction, должны идти раньше общего
handle_voice — иначе голосовое с правкой поймает общий хендлер и разберёт
правку как новое дело (ровно та ошибка, ради которой существует
services/parse_flow.py, см. его docstring).

"Без спама, не 5 сообщений разом" (Промпт 3, п.4) реализовано не только как
"один текст на запись", а с небольшой паузой между отправками: сообщения,
пришедшие в чат в одну и ту же секунду, читаются человеком как одна пачка
независимо от того, что технически это разные API-вызовы.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.keyboards import CONFIRM_PREFIX, CORRECT_PREFIX, confirmation_keyboard
from bot.state import VoiceFlow, forget, new_attempt_id, remember
from bot.state import get as get_pending
from bot.state import update as update_pending
from db.session import session_scope
from services import timeframe
from services.audio import AudioConversionError, ogg_to_wav
from services.items import save_parsed_item
from services.llm import LLMClient
from services.parse_flow import (
    FlowState,
    IllegalTransition,
    ParseAttempt,
    apply_correction,
    confirm,
    confirmation_text,
    describe_item,
    request_correction,
    split_attempts,
)
from services.transcription import TranscriptionClient, TranscriptionError
from services.users import get_or_create_user
from services.voice_parsing import parse_transcript

router = Router(name="voice")
logger = logging.getLogger(__name__)

# Пауза между последовательными подтверждениями одного голосового. Не защита
# от рейт-лимита Telegram (там запас на порядки больше), а UX: без паузы
# пять сообщений приходят одним пакетом и читаются как спам, с паузой — как
# то, что бот "разбирает по одному".
SEND_DELAY_SECONDS = 0.35


async def _transcribe_voice(message: Message, transcription_client: TranscriptionClient) -> str | None:
    """
    Скачать голосовое и получить текст. None — если обработать не вышло:
    сообщение об ошибке уже отправлено, вызывающему коду продолжать не нужно.
    """
    raw = await message.bot.download(message.voice.file_id)
    try:
        wav = await ogg_to_wav(raw.read())
        transcript = await transcription_client.transcribe(audio=wav, mime_type="audio/wav")
    except (AudioConversionError, TranscriptionError):
        # Подробности — в лог, человеку — человеческий текст. Раньше сюда
        # уезжал сырой ответ провайдера целиком, вида
        # "Error code: 400 - {'error': {'code': 400, 'message': 'User location
        # is not supported...'}}" — увидено в реальной переписке.
        logger.exception("Не удалось получить текст голосового")
        await message.answer(
            "Не получилось разобрать голосовое: сервис распознавания сейчас недоступен. "
            "Попробуйте позже или напишите текстом."
        )
        return None

    if not transcript.strip():
        await message.answer("Не расслышал. Попробуйте сказать ещё раз или напишите текстом.")
        return None
    return transcript


async def _send_attempt(bot: Bot, chat_id: int, attempt: ParseAttempt) -> None:
    """
    Отправить одну карточку подтверждения.

    id генерируется до отправки (нужен в callback_data клавиатуры), а
    запоминается — после (нужен настоящий message_id, чтобы потом
    отредактировать именно это сообщение).
    """
    attempt_id = new_attempt_id()
    sent = await bot.send_message(
        chat_id, confirmation_text(attempt), reply_markup=confirmation_keyboard(attempt_id)
    )
    remember(attempt_id, attempt, chat_id=chat_id, message_id=sent.message_id)


async def _send_attempts(bot: Bot, chat_id: int, attempts: list[ParseAttempt]) -> None:
    for index, attempt in enumerate(attempts):
        if index > 0:
            await asyncio.sleep(SEND_DELAY_SECONDS)
        await _send_attempt(bot, chat_id, attempt)


@router.message(VoiceFlow.awaiting_correction, F.voice)
async def handle_correction_voice(
    message: Message,
    state: FSMContext,
    llm_client: LLMClient,
    transcription_client: TranscriptionClient,
) -> None:
    """Правка голосом. Зарегистрирован раньше handle_voice — см. docstring модуля."""
    correction = await _transcribe_voice(message, transcription_client)
    if correction is None:
        return
    await _apply_correction(message, state, llm_client, correction)


@router.message(VoiceFlow.awaiting_correction, F.text, ~F.text.startswith("/"))
async def handle_correction_text(message: Message, state: FSMContext, llm_client: LLMClient) -> None:
    """
    Правка текстом.

    Команды сюда не попадают (~F.text.startswith("/")): с Промпта 6 у бота есть
    меню команд, и человек, зависший в режиме правки, должен иметь возможность
    просто нажать /list — а не увидеть, как его команда уезжает в модель как
    уточнение к записи.
    """
    await _apply_correction(message, state, llm_client, message.text or "")


@router.message(F.voice)
async def handle_voice(
    message: Message,
    llm_client: LLMClient,
    transcription_client: TranscriptionClient,
) -> None:
    """Новое голосовое (не правка — иначе его перехватил бы handle_correction_voice)."""
    transcript = await _transcribe_voice(message, transcription_client)
    if transcript is None:
        return
    await _handle_transcript(message, transcript, voice_file_id=message.voice.file_id, llm_client=llm_client)


@router.message(F.text, ~F.text.startswith("/"))
async def handle_text(message: Message, llm_client: LLMClient) -> None:
    """
    Запись обычным текстом — тот же путь, что и голосовое, минус скачивание и
    транскрипция.

    Регистрируется последним из текстовых: правка (в состоянии awaiting_correction)
    и все команды разбираются раньше. Команды дополнительно отсечены фильтром —
    иначе опечатка в команде уехала бы в модель как новая запись.
    """
    await _handle_transcript(message, message.text or "", voice_file_id=None, llm_client=llm_client)


async def _handle_transcript(
    message: Message, transcript: str, *, voice_file_id: str | None, llm_client: LLMClient
) -> None:
    """
    Общая часть голосового и текстового ввода: разобрать и показать карточки.

    voice_file_id есть только у голосового — у текстовой записи переслушивать
    нечего, и в базе останется NULL (db/models.py это допускает), а сам текст
    всё равно сохранится в source_transcript.
    """
    async with session_scope() as session:
        user = await get_or_create_user(session, message.from_user.id)
        user_timezone = user.timezone

    today = timeframe.today(user_timezone)
    result = await parse_transcript(transcript, client=llm_client, today=today, timezone=user_timezone)

    if not result.items:
        # Сохранять нечего — переиспользуем текст failed-состояния из
        # parse_flow, не создавая для этого отдельную попытку в bot/state.py:
        # подтверждать (и, значит, помнить) здесь нечего.
        failed = ParseAttempt(
            transcript=transcript,
            state=FlowState.failed,
            issue=result.issue,
            detail=result.detail,
            voice_file_id=voice_file_id,
        )
        await message.answer(confirmation_text(failed))
        return

    attempts = split_attempts(transcript, result.items, voice_file_id=voice_file_id)
    await _send_attempts(message.bot, message.chat.id, attempts)


@router.callback_query(F.data.startswith(CONFIRM_PREFIX))
async def handle_confirm(callback: CallbackQuery) -> None:
    attempt_id = callback.data[len(CONFIRM_PREFIX):]
    pending = get_pending(attempt_id)
    if pending is None:
        await callback.answer("Эта карточка уже не активна.", show_alert=True)
        return

    try:
        confirmed = confirm(pending.attempt)
    except IllegalTransition:
        await callback.answer("Уже обработано.", show_alert=True)
        return

    async with session_scope() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        for item in confirmed.saveable_items:
            await save_parsed_item(
                session, user, item,
                source_transcript=confirmed.transcript,
                voice_file_id=confirmed.voice_file_id,
            )

    forget(attempt_id)
    await callback.message.edit_text("✅ Сохранено\n" + describe_item(confirmed.items[0]))
    await callback.answer()


@router.callback_query(F.data.startswith(CORRECT_PREFIX))
async def handle_correct_request(callback: CallbackQuery, state: FSMContext) -> None:
    attempt_id = callback.data[len(CORRECT_PREFIX):]
    pending = get_pending(attempt_id)
    if pending is None:
        await callback.answer("Эта карточка уже не активна.", show_alert=True)
        return

    try:
        attempt = request_correction(pending.attempt)
    except IllegalTransition:
        await callback.answer("Уже обработано.", show_alert=True)
        return

    update_pending(attempt_id, attempt)
    await state.set_state(VoiceFlow.awaiting_correction)
    await state.update_data(attempt_id=attempt_id)

    original = callback.message.text or ""
    await callback.message.edit_text(f"{original}\n\nЖду правку — текстом или голосом.")
    await callback.answer()


async def _apply_correction(message: Message, state: FSMContext, llm_client: LLMClient, correction: str) -> None:
    """
    Общая часть handle_correction_voice и handle_correction_text: применить
    правку к попытке, на которую указывает FSM-состояние текущего чата.

    Одно ограничение отсюда и вытекает: "жду правку" — состояние на чат, а не
    на карточку, поэтому если пользователь нажал [Исправить] на нескольких
    карточках подряд, следующее сообщение относится только к последней —
    известное упрощение MVP (см. docstring bot/state.py).
    """
    data = await state.get_data()
    attempt_id = data.get("attempt_id")
    pending = get_pending(attempt_id) if attempt_id else None
    await state.clear()

    if pending is None:
        await message.answer("Не нашёл, к какой карточке относится правка — начните заново с голосового.")
        return

    correction = correction.strip()
    if not correction:
        await message.answer("Не понял правку — пришлите текст или голосовое ещё раз.")
        # Незавершённая правка теряется: пользователь возвращается к тому же
        # экрану, что и до нажатия [Исправить], без спец-логики восстановления.
        return

    async with session_scope() as session:
        user = await get_or_create_user(session, message.from_user.id)
        user_timezone = user.timezone

    today = timeframe.today(user_timezone)
    try:
        new_attempt = await apply_correction(
            pending.attempt, correction, client=llm_client, today=today, timezone=user_timezone
        )
    except (IllegalTransition, ValueError) as exc:
        await message.answer(f"Не получилось применить правку: {exc}")
        return

    if new_attempt.state is FlowState.failed:
        # Правки кончились (MAX_CORRECTION_ROUNDS) или провайдер лёг — дальше
        # переспрашивать нечестно, предлагаем начать заново.
        forget(attempt_id)
        await message.bot.edit_message_text(
            confirmation_text(new_attempt), chat_id=pending.chat_id, message_id=pending.message_id
        )
        return

    # Правка могла разойтись на несколько записей (редко, но
    # parse_transcript этого не запрещает) — раскладываем тем же split_attempts,
    # первую кладём на место старой карточки, остальные шлём новыми.
    new_split = split_attempts(new_attempt.transcript, new_attempt.items, voice_file_id=new_attempt.voice_file_id)
    first, rest = new_split[0], new_split[1:]

    update_pending(attempt_id, first)
    await message.bot.edit_message_text(
        confirmation_text(first),
        chat_id=pending.chat_id,
        message_id=pending.message_id,
        reply_markup=confirmation_keyboard(attempt_id),
    )
    await _send_attempts(message.bot, pending.chat_id, rest)
