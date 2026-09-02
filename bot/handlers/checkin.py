"""
Вечерний чек-ин: рассылка по расписанию (bot/scheduler.py дёргает раз в час)
и три хендлера кнопок под каждой задачей.

Разбивка ответственности:
* services/checkin.py решает, КОМУ сейчас пора (таймзона-осознанно);
* services/items.py (evening_checkin_tasks) решает, ЧТО показать — сегодняшние
  pending-задачи вместе с накопившейся просрочкой (см. её же docstring и
  комментарий у ix_items_user_pending_tasks в db/models.py — индекс так и
  назван под этот сценарий);
* этот модуль — только Telegram-бухгалтерия: как отправить и как обработать нажатие.

Формат сообщений: одна карточка на задачу (текст + своя клавиатура), а не
одно сообщение с несколькими строками кнопок на всех сразу. Так проще и
надёжнее показывать "Перенесено"/"Удалено" именно под той задачей, на
которую нажали, не трогая остальные, — тот же приём, что и в
bot/handlers/voice.py для подтверждений разбора.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards import (
    CHECKIN_DELETE_PREFIX,
    CHECKIN_KEEP_PREFIX,
    CHECKIN_POSTPONE_PREFIX,
    checkin_task_keyboard,
)
from db.models import Item, ItemStatus, User
from db.session import session_scope
from services import checkin as checkin_svc
from services import items as items_svc
from services import timeframe

router = Router(name="checkin")
logger = logging.getLogger(__name__)

CHECKIN_INTRO = "Как прошёл день? Что делаем с невыполненным?"

# Пауза перед каждой следующей карточкой задачи одного пользователя — тот же
# приём и то же значение, что SEND_DELAY_SECONDS в bot/handlers/voice.py:
# без неё несколько карточек одного чек-ина приходят одним пакетом.
TASK_DELAY_SECONDS = 0.35

# "Пачками с паузой, а не все сразу" (Промпт 4, п.4) — это уже про масштаб
# рассылки по ВСЕМ пользователям сразу, а не про карточки одного человека
# (для них хватает TASK_DELAY_SECONDS выше). Внутри пачки пользователи всё
# равно рассылаются последовательно, а не параллельно — сама эта
# последовательность уже ограничивает пиковую частоту; пауза между пачками —
# дополнительный запас на случай, если аудитория вырастет и одной
# последовательной рассылки станет мало.
USER_BATCH_SIZE = 20
USER_BATCH_PAUSE_SECONDS = 1.0


def _describe_task(item: Item, today: dt.date) -> str:
    """Строка карточки: обычная задача — просто заголовок, просроченная — с пометкой."""
    if item.due_date is not None and item.due_date < today:
        days_late = (today - item.due_date).days
        return f"{item.title} (просрочено на {days_late} дн.)"
    return item.title


async def send_checkin(bot: Bot, user: User, tasks: list[Item]) -> None:
    """Отправить одному пользователю вступление и карточку на каждую задачу."""
    if not tasks:
        return

    today = timeframe.today(user.timezone)
    await bot.send_message(user.telegram_id, CHECKIN_INTRO)

    for task in tasks:
        await asyncio.sleep(TASK_DELAY_SECONDS)
        await bot.send_message(
            user.telegram_id,
            _describe_task(task, today),
            reply_markup=checkin_task_keyboard(task.id),
        )


async def run_checkin_broadcast(bot: Bot, *, at_utc: dt.datetime | None = None) -> int:
    """
    Разослать вечерний чек-ин всем, у кого прямо сейчас 20:00 по их поясу.

    Возвращает число пользователей, которым реально что-то ушло (пусто у
    задач — сообщение не шлём: дёргать человека вечером, когда разбирать
    нечего, — не забота, а лишнее уведомление).
    """
    at_utc = at_utc or dt.datetime.now(tz=timeframe.UTC)

    # Всё чтение — в одной короткой сессии, до начала отправки: рассылка
    # медленная (паузы, сеть), держать ради неё открытое соединение с БД незачем.
    async with session_scope() as session:
        users = await checkin_svc.due_users(session, at_utc)
        tasks_by_user = {user.id: await items_svc.evening_checkin_tasks(session, user) for user in users}

    sent = 0
    for batch_start in range(0, len(users), USER_BATCH_SIZE):
        batch = users[batch_start : batch_start + USER_BATCH_SIZE]
        for user in batch:
            tasks = tasks_by_user.get(user.id, [])
            if not tasks:
                continue
            try:
                await send_checkin(bot, user, tasks)
                sent += 1
            except Exception:
                # Один пользователь, у которого бот заблокирован или сеть
                # моргнула, не должен обрывать рассылку остальным.
                logger.exception("Не удалось отправить чек-ин user_id=%s", user.id)

        if batch_start + USER_BATCH_SIZE < len(users):
            await asyncio.sleep(USER_BATCH_PAUSE_SECONDS)

    return sent


async def _load_owned_pending_item(session: AsyncSession, item_id: int, telegram_id: int) -> Item | None:
    """
    Задача по id, но только если она ещё pending и принадлежит именно этому
    telegram-пользователю — на случай подделанного callback_data и повторного
    нажатия (задачу уже перенесли/удалили — action больше не применим).
    """
    stmt = (
        select(Item)
        .join(User, Item.user_id == User.id)
        .where(Item.id == item_id, User.telegram_id == telegram_id, Item.status == ItemStatus.pending)
    )
    return (await session.scalars(stmt)).one_or_none()


@router.callback_query(F.data.startswith(CHECKIN_POSTPONE_PREFIX))
async def handle_postpone(callback: CallbackQuery) -> None:
    item_id = int(callback.data[len(CHECKIN_POSTPONE_PREFIX) :])

    async with session_scope() as session:
        item = await _load_owned_pending_item(session, item_id, callback.from_user.id)
        if item is None:
            await callback.answer("Эта задача уже не актуальна.", show_alert=True)
            return
        user = (await session.scalars(select(User).where(User.telegram_id == callback.from_user.id))).one()
        item.due_date = timeframe.today(user.timezone) + dt.timedelta(days=1)

    await callback.message.edit_text(f"{callback.message.text}\n\n➡️ Перенесено на завтра.")
    await callback.answer()


@router.callback_query(F.data.startswith(CHECKIN_KEEP_PREFIX))
async def handle_keep(callback: CallbackQuery) -> None:
    # "Оставить" не меняет данные — задача остаётся pending с той же датой,
    # обновлять в базе нечего. Транзакция (session_scope) здесь и не нужна:
    # п.3 промпта требует её "на каждое обновление", а обновления нет.
    item_id = int(callback.data[len(CHECKIN_KEEP_PREFIX) :])
    await callback.message.edit_text(f"{callback.message.text}\n\n📌 Оставлено как есть.")
    await callback.answer()


@router.callback_query(F.data.startswith(CHECKIN_DELETE_PREFIX))
async def handle_delete(callback: CallbackQuery) -> None:
    item_id = int(callback.data[len(CHECKIN_DELETE_PREFIX) :])

    async with session_scope() as session:
        item = await _load_owned_pending_item(session, item_id, callback.from_user.id)
        if item is None:
            await callback.answer("Эта задача уже не актуальна.", show_alert=True)
            return
        # Мягкое удаление (см. db/models.py ItemStatus) — транскрипт и история
        # остаются, задача просто перестаёт быть pending.
        item.status = ItemStatus.deleted

    await callback.message.edit_text(f"{callback.message.text}\n\n🗑 Удалено.")
    await callback.answer()
