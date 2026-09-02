"""
Настройки (Промпт 6, п.6).

Пока единственный пункт — час вечернего чек-ина: до этого шага он был
константой в коде (services/checkin.CHECKIN_HOUR), теперь хранится в
users.checkin_hour.

TODO (следующий шаг настроек): выбор часового пояса. Сейчас все пользователи
считаются московскими (services/users.DEFAULT_TIMEZONE), поэтому "20:00 по
вашему времени" и "20:00 МСК" — одно и то же. Как только пояс станет
настраиваемым, сюда добавится второй пункт, а формулировки в текстах уже
написаны так, чтобы их не пришлось переписывать.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot import views
from bot.callbacks import SettingsCB
from db.session import session_scope
from services.users import get_or_create_user

router = Router(name="settings")


@router.message(Command("settings"))
async def handle_settings(message: Message) -> None:
    async with session_scope() as session:
        user = await get_or_create_user(session, message.from_user.id)

    await message.answer(views.settings_text(user), reply_markup=views.settings_keyboard(user))


@router.callback_query(SettingsCB.filter(F.act == "hm"))
async def handle_hours_menu(callback: CallbackQuery) -> None:
    async with session_scope() as session:
        user = await get_or_create_user(session, callback.from_user.id)

    await views.edit_view(
        callback.message,
        "Во сколько присылать вечерний чек-ин?",
        views.hours_keyboard(user),
    )
    await callback.answer()


@router.callback_query(SettingsCB.filter(F.act == "h"))
async def handle_set_hour(callback: CallbackQuery, callback_data: SettingsCB) -> None:
    async with session_scope() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        user.checkin_hour = callback_data.val
        # user нужен ниже уже с новым значением — сессия закроется, но
        # expire_on_commit=False (db/session.py) оставляет объект пригодным.

    await views.edit_view(callback.message, views.settings_text(user), views.settings_keyboard(user))
    await callback.answer(f"Чек-ин в {callback_data.val:02d}:00")


@router.callback_query(SettingsCB.filter(F.act == "b"))
async def handle_back(callback: CallbackQuery) -> None:
    async with session_scope() as session:
        user = await get_or_create_user(session, callback.from_user.id)

    await views.edit_view(callback.message, views.settings_text(user), views.settings_keyboard(user))
    await callback.answer()
