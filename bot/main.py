"""
Точка входа: бот поднимается, отвечает на /start, принимает голосовые и раз в
час рассылает вечерний чек-ин тем, у кого сейчас 20:00 по их поясу.

Запуск из корня проекта: python -m bot.main
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from bot.handlers import checkin, start, voice
from bot.scheduler import hourly_checkin_loop
from config import require_bot_token
from services.llm import OpenAICompatibleClient


def create_dispatcher() -> Dispatcher:
    # MemoryStorage — FSM-состояние ("жду правку") живёт в памяти процесса, как
    # и bot/state.py: перезапуск бота теряет незавершённые правки, но не
    # данные в базе (см. docstring bot/state.py).
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(start.router)
    dp.include_router(voice.router)
    dp.include_router(checkin.router)
    return dp


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=require_bot_token())
    dp = create_dispatcher()

    # Один клиент на процесс, переданный в хендлеры как llm_client и
    # transcription_client: у OpenAICompatibleClient оба протокола (разбор и
    # транскрипция), но хендлерам это неважно — они видят только Protocol'ы
    # LLMClient/TranscriptionClient (services/llm.py, services/transcription.py).
    # start_polling прокидывает именованные kwargs в хендлеры как
    # dependency injection.
    client = OpenAICompatibleClient()

    # Часовой чек-ин живёт фоновой задачей рядом с polling'ом (см.
    # bot/scheduler.py) — при остановке бота его нужно снять явно, иначе
    # asyncio пожалуется на незавершённую задачу при выходе из процесса.
    checkin_task = asyncio.create_task(hourly_checkin_loop(bot))

    logging.info("Бот запущен, начинаю polling")
    try:
        await dp.start_polling(bot, llm_client=client, transcription_client=client)
    finally:
        checkin_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
