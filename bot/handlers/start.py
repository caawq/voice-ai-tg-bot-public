"""Обработчик /start. Пока просто подтверждает, что бот жив — без БД и без ИИ."""

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

router = Router(name="start")


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    await message.answer(
        "Привет! Я на связи. Пока умею только это сообщение-заглушку — "
        "скоро научусь понимать твои голосовые."
    )
