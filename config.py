"""
Минимальный конфиг: секреты берутся только из переменных окружения / .env.
Ничего секретного не хардкодим и не коммитим (см. .env.example, .gitignore).
"""

import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")


def require_bot_token() -> str:
    """Падаем сразу и понятно, если токен не настроен, а не глубоко внутри aiogram."""
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN не задан. Скопируй .env.example в .env и укажи токен бота "
            "(получить у @BotFather)."
        )
    return BOT_TOKEN
