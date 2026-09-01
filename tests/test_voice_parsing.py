"""
Разбор расшифровок в записи — на настоящих фразах, которые люди наговаривают.

Ни бота, ни сети, ни базы: парсер получает фейкового провайдера и фиксированную
дату «сегодня». Это и есть проверка того, что бизнес-логика отделена от Telegram.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from conftest import TIMEZONE, TODAY, FakeLLM, item, payload
from db.models import ItemType
from services.llm import LLMError
from services.voice_parsing import ParseIssue, parse_transcript

TOMORROW = (TODAY + dt.timedelta(days=1)).isoformat()


def run(coro):
    return asyncio.run(coro)


def parse(llm: FakeLLM, text: str, **kwargs):
    return run(parse_transcript(text, client=llm, today=TODAY, timezone=TIMEZONE, **kwargs))


def test_одна_фраза_одно_событие():
    """«Напомни завтра в 15 позвонить клиенту» — событие с датой и часом."""
    llm = FakeLLM(payload(item("event", "позвонить клиенту", date=TOMORROW, time="15:00", confidence=0.96)))

    result = parse(llm, "напомни завтра в 15 позвонить клиенту")

    assert result.is_clean
    assert len(result.items) == 1
    only = result.items[0]
    assert only.type is ItemType.event
    assert only.label == "позвонить клиенту"
    assert only.date == TODAY + dt.timedelta(days=1)
    assert only.time == dt.time(15, 0)
    assert only.goal_progress is None


def test_многосоставное_голосовое_даёт_несколько_записей():
    """«Напомни вечером позвонить маме и купи корм коту» — два разных дела, а не одно."""
    llm = FakeLLM(
        payload(
            item("event", "позвонить маме", date=TOMORROW, time="19:00", confidence=0.78),
            item("task", "купить корм коту", date=TOMORROW, confidence=0.93),
        )
    )

    result = parse(llm, "напомни завтра вечером позвонить маме и купи корм коту")

    assert result.is_clean
    assert [i.type for i in result.items] == [ItemType.event, ItemType.task]
    assert [i.label for i in result.items] == ["позвонить маме", "купить корм коту"]
    # У задачи нет часа — это и отличает её от события.
    assert result.items[1].time is None
    # Уверенность результата — по самой слабой записи, а не средняя: «вечером»
    # бот угадал часом, и это не должно теряться в среднем.
    assert result.min_confidence == pytest.approx(0.78)


def test_цель_без_даты_с_прогрессом():
    """«Хочу за месяц подтянуть английский» — цель, а не задача с дедлайном."""
    llm = FakeLLM(payload(item("goal", "подтянуть английский", goal_progress=0, confidence=0.88)))

    result = parse(llm, "хочу за месяц подтянуть английский")

    assert result.is_clean
    goal = result.items[0]
    assert goal.type is ItemType.goal
    assert goal.goal_progress == 0
    assert goal.date is None and goal.time is None


def test_цель_с_придуманной_датой_теряет_дату_а_не_запись():
    """Модель дописала цели дату — молча выбрасываем дату, запись остаётся целью."""
    llm = FakeLLM(payload(item("goal", "подтянуть английский", date=TOMORROW, goal_progress=10)))

    result = parse(llm, "хочу подтянуть английский")

    assert result.is_clean
    assert result.items[0].date is None
    assert result.items[0].goal_progress == 10


def test_задача_с_точным_часом_становится_событием():
    """Запись с конкретным часом — по схеме проекта событие, как бы её ни назвала модель."""
    llm = FakeLLM(payload(item("task", "забрать посылку", date=TOMORROW, time="18:30")))

    result = parse(llm, "завтра в полседьмого забрать посылку")

    assert result.items[0].type is ItemType.event
    assert result.items[0].time == dt.time(18, 30)


def test_низкая_уверенность_не_сохраняется_молча():
    """Разобрали, но не уверены — результат есть, но помечен как требующий явного да."""
    llm = FakeLLM(payload(item("task", "разобрать балкон", confidence=0.42)))

    result = parse(llm, "ну надо бы этот балкон когда-нибудь")

    assert result.issue is ParseIssue.low_confidence
    assert not result.is_clean
    assert result.items, "записи не выбрасываем — их показывают человеку на подтверждение"


def test_событие_без_времени_считается_битым_ответом():
    """Схема провайдера гарантирует форму, но не смысл: событие без часа — не событие."""
    llm = FakeLLM(payload(item("event", "созвон", date=TOMORROW, time=None)))

    result = parse(llm, "созвон завтра")

    assert result.issue is ParseIssue.invalid_model_response
    assert "без даты или времени" in result.detail


def test_кривая_дата_не_ломает_бота():
    llm = FakeLLM(payload(item("task", "купить корм", date="завтра")))

    result = parse(llm, "купить корм завтра")

    assert result.issue is ParseIssue.invalid_model_response
    assert "YYYY-MM-DD" in result.detail


def test_провайдер_недоступен():
    """Сеть, лимит или неверный ключ — не исключение наружу, а понятный статус."""
    llm = FakeLLM(error=LLMError("429 rate limit"))

    result = parse(llm, "напомни завтра позвонить")

    assert result.issue is ParseIssue.provider_unavailable
    assert "429" in result.detail


def test_пустая_расшифровка_не_дёргает_модель():
    llm = FakeLLM()

    result = parse(llm, "   ")

    assert result.issue is ParseIssue.nothing_recognized
    assert llm.calls == [], "на пустой текст незачем тратить вызов провайдера"


def test_модель_получает_сегодняшнюю_дату_пользователя():
    """«Завтра» модель может посчитать только зная, какое сегодня у пользователя."""
    llm = FakeLLM(payload(item("task", "купить корм", date=TOMORROW)))

    parse(llm, "купить корм завтра")

    system = llm.calls[0]["system"]
    assert TODAY.isoformat() in system
    assert "понедельник" in system
    assert TIMEZONE in system


def test_схема_пригодна_для_strict_режима():
    """
    Strict structured output у провайдера включается только если схема
    соответствует его требованиям: все поля обязательные, лишние запрещены.
    Проверяем это здесь, а не на живом вызове — там ошибка стоила бы 400-ки.
    """
    from services.voice_parsing import ITEMS_SCHEMA

    def walk(node, path="root"):
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, f"{path}: разрешены лишние поля"
            assert set(node.get("required", [])) == set(node["properties"]), (
                f"{path}: required должен перечислять все свойства"
            )
            for name, child in node["properties"].items():
                walk(child, f"{path}.{name}")
        elif node.get("type") == "array":
            walk(node["items"], f"{path}[]")

    walk(ITEMS_SCHEMA)
