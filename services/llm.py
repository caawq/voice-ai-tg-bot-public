"""
Доступ к LLM: единственное место в проекте, которое знает про провайдера.

Провайдер выбран OpenAI-совместимый — не ради самой OpenAI, а потому что этим
протоколом говорят почти все: OpenRouter, proxyapi.ru, DeepSeek, локальные
модели через vLLM/Ollama. Смена провайдера — это правка LLM_BASE_URL и
LLM_MODEL в .env, а не переписывание кода. Через OpenRouter тем же протоколом
доступен и Claude, так что выбор ничего не закрывает.

Структура ответа берётся нативным механизмом провайдера, а не просьбой
"верни JSON текстом":

* ``json_schema`` (по умолчанию) — strict structured output: провайдер сам
  гарантирует, что ответ соответствует схеме, модель физически не может
  вернуть лишнее поле или забыть обязательное;
* ``tools`` — function calling с принудительным вызовом одной функции. Тот же
  результат, но поддерживается более широким кругом совместимых эндпоинтов;
  переключается LLM_STRUCTURED_MODE=tools, если ваш провайдер не умеет первое.

Модуль намеренно не знает ни про Telegram, ни про формат записей бота: на
вход — сообщения и схема, на выходе — словарь. Всё остальное в
services/voice_parsing.py.
"""

from __future__ import annotations

from typing import Any, Protocol

import config


class LLMError(RuntimeError):
    """Провайдер не ответил или ответил не тем. Ловится вызывающим кодом."""


class LLMClient(Protocol):
    """
    Контракт клиента. В тестах подменяется фейком — именно поэтому парсер
    принимает клиента параметром, а не создаёт его сам.
    """

    async def structured_call(
        self, *, system: str, messages: list[dict[str, str]], schema: dict[str, Any], schema_name: str
    ) -> dict[str, Any]:
        """Вернуть словарь, соответствующий schema. Бросить LLMError, если не вышло."""
        ...


class OpenAICompatibleClient:
    """Клиент к любому OpenAI-совместимому эндпоинту."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        structured_mode: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        # Импорт внутри конструктора: тесты работают с фейковым клиентом, и
        # тянуть ради них SDK в окружение незачем.
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover
            raise LLMError("Не установлен пакет openai: pip install -r requirements.txt") from exc

        self.model = model or config.LLM_MODEL
        self.structured_mode = structured_mode or config.LLM_STRUCTURED_MODE
        self._client = AsyncOpenAI(
            api_key=api_key or config.require_llm_api_key(),
            base_url=base_url or config.LLM_BASE_URL,
            timeout=timeout,
        )

    async def structured_call(
        self, *, system: str, messages: list[dict[str, str]], schema: dict[str, Any], schema_name: str
    ) -> dict[str, Any]:
        import json

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            # Разбор голосового — задача на извлечение фактов, а не на
            # сочинительство: температура ноль, чтобы одна и та же фраза
            # разбиралась одинаково.
            "temperature": 0,
        }

        if self.structured_mode == "tools":
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {"name": schema_name, "parameters": schema, "strict": True},
                }
            ]
            payload["tool_choice"] = {"type": "function", "function": {"name": schema_name}}
        else:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": schema, "strict": True},
            }

        try:
            response = await self._client.chat.completions.create(**payload)
        except Exception as exc:  # сеть, лимиты, неверный ключ
            raise LLMError(f"Провайдер не ответил: {exc}") from exc

        choice = response.choices[0]
        if self.structured_mode == "tools":
            calls = choice.message.tool_calls or []
            if not calls:
                raise LLMError("Модель не вызвала функцию, хотя вызов был обязательным")
            raw = calls[0].function.arguments
        else:
            # refusal — штатный отказ модели, а не сбой: возвращаем как ошибку,
            # чтобы верхний слой попросил подтверждение, а не молча сохранил.
            if getattr(choice.message, "refusal", None):
                raise LLMError(f"Модель отказалась разбирать сообщение: {choice.message.refusal}")
            raw = choice.message.content or ""

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Ответ провайдера не разобрался как JSON: {raw[:200]!r}") from exc

        if not isinstance(parsed, dict):
            raise LLMError(f"Ожидался объект, пришло {type(parsed).__name__}")
        return parsed
