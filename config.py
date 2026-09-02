"""
Минимальный конфиг: секреты берутся только из переменных окружения / .env.
Ничего секретного не хардкодим и не коммитим (см. .env.example, .gitignore).
"""

import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
# Любой OpenAI-совместимый эндпоинт: сама OpenAI, OpenRouter, proxyapi.ru,
# DeepSeek, локальная модель. Меняется только эта строка и имя модели.
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")
# json_schema — strict structured output (надёжнее); tools — function calling
# (поддерживается более широким кругом совместимых провайдеров).
LLM_STRUCTURED_MODE = os.environ.get("LLM_STRUCTURED_MODE", "json_schema")
# Модель для транскрипции голосовых. По умолчанию — та же, что и для
# разбора (LLM_MODEL); переопределить есть смысл только если у провайдера
# распознавание речи и структурирование текста — разные модели.
TRANSCRIBE_MODEL = os.environ.get("TRANSCRIBE_MODEL", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")


def require_bot_token() -> str:
    """Падаем сразу и понятно, если токен не настроен, а не глубоко внутри aiogram."""
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN не задан. Скопируй .env.example в .env и укажи токен бота "
            "(получить у @BotFather)."
        )
    return BOT_TOKEN


def require_database_url() -> str:
    """
    Строка подключения к БД, приведённая к асинхронному драйверу.

    Хостинги (Railway, Render, Heroku и прочие) отдают DATABASE_URL в виде
    postgres://... или postgresql://... — оба варианта уедут в синхронный
    драйвер и упадут на первом же запросе. Чиним здесь один раз, а не ловим
    потом на деплое.
    """
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL не задан. Скопируй .env.example в .env и укажи строку "
            "подключения к PostgreSQL."
        )
    url = DATABASE_URL
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix):]
    return url


def require_llm_api_key() -> str:
    """Ключ LLM-провайдера. Падаем на старте, а не в момент первого голосового."""
    if not LLM_API_KEY:
        raise RuntimeError(
            "LLM_API_KEY не задан. Скопируй .env.example в .env и укажи ключ "
            "провайдера (см. LLM_BASE_URL — он должен быть от того же провайдера)."
        )
    return LLM_API_KEY
