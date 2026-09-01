"""Общее для тестов: фейковый LLM-клиент и фиксированная «сегодня»."""

from __future__ import annotations

import datetime as dt
import pathlib
import sys
from typing import Any

# Корень проекта в sys.path — чтобы тесты запускались просто `pytest` из корня.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services.llm import LLMError  # noqa: E402

# Понедельник. Все ожидаемые даты в тестах считаются от него.
TODAY = dt.date(2026, 8, 31)
TIMEZONE = "Europe/Moscow"


class FakeLLM:
    """
    Подстава вместо провайдера.

    Отдаёт заранее заготовленные ответы по очереди и записывает всё, с чем её
    позвали, — чтобы проверять не только результат, но и то, ЧТО именно ушло в
    модель (в частности, попал ли в правку контекст прошлой попытки).
    """

    def __init__(self, *payloads: dict[str, Any], error: Exception | None = None) -> None:
        self.payloads = list(payloads)
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def structured_call(
        self, *, system: str, messages: list[dict[str, str]], schema: dict[str, Any], schema_name: str
    ) -> dict[str, Any]:
        self.calls.append(
            {"system": system, "messages": messages, "schema": schema, "schema_name": schema_name}
        )
        if self.error is not None:
            raise self.error
        if not self.payloads:
            raise AssertionError("FakeLLM позвали больше раз, чем заготовлено ответов")
        return self.payloads.pop(0)


def item(
    type: str,
    label: str,
    *,
    date: str | None = None,
    time: str | None = None,
    goal_progress: int | None = None,
    confidence: float = 0.95,
) -> dict[str, Any]:
    """Ответ модели по одной записи — ровно в том виде, в каком его отдаёт схема."""
    return {
        "type": type,
        "label": label,
        "date": date,
        "time": time,
        "goal_progress": goal_progress,
        "confidence_score": confidence,
    }


def payload(*items: dict[str, Any]) -> dict[str, Any]:
    return {"items": list(items)}


__all__ = ["FakeLLM", "LLMError", "TODAY", "TIMEZONE", "item", "payload"]
