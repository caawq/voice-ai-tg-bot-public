"""
Точка входа "ходячего скелета": бот поднимается и отвечает на /start.
Без базы, без ИИ — только чтобы убедиться, что деплой и токен настроены верно.

Запуск из корня проекта: python -m bot.main
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher

from bot.handlers import start
from config import require_bot_token


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(start.router)
    return dp


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=require_bot_token())
    dp = create_dispatcher()
    logging.info("Бот запущен, начинаю polling")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
