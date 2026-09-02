"""
Проверка связи с LLM-провайдером — одной командой, без Telegram.

    docker compose exec bot python scripts/check_llm.py

Отвечает на единственный вопрос: доходит ли запрос из контейнера до
провайдера. Нужен потому, что через бота этот же вопрос выясняется долго
(записать голосовое, дождаться ответа, прочитать текст ошибки), а причин
у молчания много: ключ, модель, сеть, география.
"""

from __future__ import annotations

import asyncio
import sys

import config
from services.llm import LLMError, OpenAICompatibleClient

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ok"],
    "properties": {"ok": {"type": "boolean"}},
}


async def main() -> int:
    print("Провайдер :", config.LLM_BASE_URL)
    print("Модель    :", config.LLM_MODEL)
    print("Ключ      :", "задан" if config.LLM_API_KEY else "НЕ ЗАДАН")
    print("Прокси    :", config.LLM_PROXY or "нет (идём напрямую)")
    print()

    client = OpenAICompatibleClient()
    try:
        await client.structured_call(
            system="Ответь ok=true.",
            messages=[{"role": "user", "content": "проверка связи"}],
            schema=SCHEMA,
            schema_name="healthcheck",
        )
    except LLMError as exc:
        text = str(exc)
        print("НЕ РАБОТАЕТ:", text)
        print()
        if "User location is not supported" in text:
            print("Это география: провайдер не обслуживает страну, из которой пришёл запрос.")
            print("Лечится только маршрутом — задайте LLM_PROXY в .env и пересоберите,")
            print("либо смените провайдера (LLM_BASE_URL + LLM_API_KEY).")
            print("Важно: VPN на самой Windows контейнеру не помогает — Docker ходит мимо.")
        elif "API key" in text or "401" in text or "403" in text:
            print("Похоже на ключ: проверьте LLM_API_KEY и что он от того же провайдера,")
            print("что и LLM_BASE_URL.")
        elif "model" in text.lower() or "404" in text:
            print("Похоже на модель: проверьте LLM_MODEL — такого имени у провайдера может не быть.")
        return 1

    print("РАБОТАЕТ: провайдер ответил, разбор голосовых и текста будет работать.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
