"""
Картинка недели: команда /week, переключение темы /theme и проактивная
еженедельная рассылка (see services/week_schedule.py — расписание) поверх
уже готового render/render_week.py (Промпт 0).

render_week_image — синхронная функция (Playwright sync API, свой блокирующий
браузерный подпроцесс на вызов), поэтому вызывается через asyncio.to_thread:
иначе рендер одной картинки блокирует event loop бота целиком, включая
polling и все остальные апдейты, на всё время рендера.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import pathlib
import tempfile

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import FSInputFile, Message

from db.models import Item, User
from db.session import session_scope
from render.render_week import render_week_image
from services import items as items_svc
from services import timeframe
from services import users as users_svc
from services import week_render, week_schedule
from services.users import get_or_create_user

router = Router(name="week")
logger = logging.getLogger(__name__)

# Тот же приём, что в bot/handlers/checkin.py: последовательная отправка
# внутри пачки плюс пауза между пачками — против rate limit Telegram при
# рассылке многим пользователям сразу. Здесь особенно кстати: рендер каждой
# картинки — это ещё и отдельный подпроцесс Chromium, гнать их пачкой без
# пауз не стоит и по нагрузке на сам сервер, не только из-за Telegram.
WEEK_USER_BATCH_SIZE = 10
WEEK_USER_BATCH_PAUSE_SECONDS = 1.5


async def send_week_image(
    bot: Bot, chat_id: int, user: User, items: list[Item], today: dt.date, monday: dt.date
) -> None:
    """Отрендерить неделю в PNG и отправить, гарантированно убрав временный файл."""
    data = week_render.build_week_data(items, monday, user.timezone, today)

    out_path = tempfile.mktemp(suffix=".png", prefix="week-")
    try:
        await asyncio.to_thread(render_week_image, data, user.theme, out_path)
        await bot.send_photo(chat_id, FSInputFile(out_path))
    finally:
        pathlib.Path(out_path).unlink(missing_ok=True)


@router.message(Command("week"))
async def handle_week(message: Message) -> None:
    async with session_scope() as session:
        user = await get_or_create_user(session, message.from_user.id)
        today = timeframe.today(user.timezone)
        monday = timeframe.week_start(today)
        items = await items_svc.week_items(session, user, monday)

    await send_week_image(message.bot, message.chat.id, user, items, today, monday)


@router.message(Command("theme"))
async def handle_theme(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip().lower()
    if arg not in users_svc.VALID_THEMES:
        await message.answer("Укажите тему: /theme light или /theme dark.")
        return

    async with session_scope() as session:
        user = await get_or_create_user(session, message.from_user.id)
        users_svc.set_theme(user, arg)

    await message.answer(f"Готово, тема картинки недели теперь {arg}.")


async def run_weekly_broadcast(bot: Bot, *, at_utc: dt.datetime | None = None) -> int:
    """
    Разослать картинку недели всем, у кого прямо сейчас наступил час
    еженедельной отправки (services/week_schedule.py) по их поясу.

    Возвращает число пользователей, которым реально ушла картинка.
    """
    at_utc = at_utc or dt.datetime.now(tz=timeframe.UTC)

    async with session_scope() as session:
        users = await week_schedule.due_users(session, at_utc)
        weeks = {}
        for user in users:
            today = timeframe.today(user.timezone)
            monday = timeframe.week_start(today)
            weeks[user.id] = (today, monday, await items_svc.week_items(session, user, monday))

    sent = 0
    for batch_start in range(0, len(users), WEEK_USER_BATCH_SIZE):
        batch = users[batch_start : batch_start + WEEK_USER_BATCH_SIZE]
        for user in batch:
            today, monday, items = weeks[user.id]
            try:
                await send_week_image(bot, user.telegram_id, user, items, today, monday)
                sent += 1
            except Exception:
                logger.exception("Не удалось отправить картинку недели user_id=%s", user.id)

        if batch_start + WEEK_USER_BATCH_SIZE < len(users):
            await asyncio.sleep(WEEK_USER_BATCH_PAUSE_SECONDS)

    return sent
