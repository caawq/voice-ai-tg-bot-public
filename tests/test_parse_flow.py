"""
Флоу подтверждения и коррекции: главное здесь — что кнопка [Исправить] ведёт
не в никуда, а к повторному разбору с контекстом прошлой попытки.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import pathlib

import pytest

from conftest import TIMEZONE, TODAY, FakeLLM, item, payload
from db.models import ItemType
from services import parse_flow
from services.llm import LLMError
from services.parse_flow import FlowState, IllegalTransition
from services.voice_parsing import ParseIssue

TOMORROW = (TODAY + dt.timedelta(days=1)).isoformat()


def run(coro):
    return asyncio.run(coro)


def start(llm: FakeLLM, text: str, **kwargs):
    return run(parse_flow.start(text, client=llm, today=TODAY, timezone=TIMEZONE, **kwargs))


def correct(attempt, llm: FakeLLM, text: str):
    return run(parse_flow.apply_correction(attempt, text, client=llm, today=TODAY, timezone=TIMEZONE))


def test_обычный_путь_до_сохранения():
    llm = FakeLLM(payload(item("event", "позвонить клиенту", date=TOMORROW, time="15:00")))

    attempt = start(llm, "напомни завтра в 15 позвонить клиенту", voice_file_id="AwACAgIAA")
    assert attempt.state is FlowState.awaiting_confirmation
    assert attempt.saveable_items == [], "до [Да] сохранять нечего"
    assert "Записал:" in parse_flow.confirmation_text(attempt)

    confirmed = parse_flow.confirm(attempt)
    assert confirmed.state is FlowState.confirmed
    assert len(confirmed.saveable_items) == 1
    assert confirmed.voice_file_id == "AwACAgIAA", "ссылка на голосовое переживает подтверждение"


def test_исправить_уточняет_прошлую_попытку_а_не_создаёт_новую():
    """
    Ключевой тест шага.

    Пользователь сказал «в 15», нажал [Исправить] и написал «не в 15, а в 17».
    Сама по себе эта фраза не содержит никакого дела — если разбирать её как
    новое сообщение, получится мусор. Проверяем, что в модель ушёл контекст
    прошлого разбора и что итог — исправленная старая запись, а не вторая новая.
    """
    llm = FakeLLM(
        payload(item("event", "позвонить клиенту", date=TOMORROW, time="15:00")),
        payload(item("event", "позвонить клиенту", date=TOMORROW, time="17:00")),
    )

    attempt = start(llm, "напомни завтра в 15 позвонить клиенту")
    waiting = parse_flow.request_correction(attempt)
    assert waiting.state is FlowState.awaiting_correction
    assert waiting.items == attempt.items, "разбор не выбрасываем — он нужен как контекст"

    fixed = correct(waiting, llm, "не в 15, а в 17")

    assert fixed.state is FlowState.awaiting_confirmation
    assert len(fixed.items) == 1, "правка меняет запись, а не добавляет вторую"
    assert fixed.items[0].time == dt.time(17, 0)
    assert fixed.corrections == ["не в 15, а в 17"]
    assert fixed.round == 2

    # А теперь — что именно ушло в модель во второй раз.
    second_call = llm.calls[1]["messages"]
    roles = [m["role"] for m in second_call]
    assert roles == ["user", "assistant", "user"]
    assert second_call[0]["content"] == "напомни завтра в 15 позвонить клиенту"
    previous = json.loads(second_call[1]["content"])
    assert previous["items"][0]["time"] == "15:00", "модель видит прошлый разбор"
    assert "не в 15, а в 17" in second_call[2]["content"]
    assert "ПРЕДЫДУЩИЙ" in second_call[2]["content"], "и знает, что это уточнение, а не новое дело"


def test_правка_может_добавить_дело_к_прошлому_разбору():
    llm = FakeLLM(
        payload(item("task", "купить корм коту", date=TOMORROW)),
        payload(
            item("task", "купить корм коту", date=TOMORROW),
            item("task", "купить наполнитель", date=TOMORROW),
        ),
    )

    attempt = start(llm, "завтра купить корм коту")
    fixed = correct(parse_flow.request_correction(attempt), llm, "и наполнитель ещё")

    assert [i.label for i in fixed.items] == ["купить корм коту", "купить наполнитель"]


def test_низкая_уверенность_требует_явного_подтверждения():
    llm = FakeLLM(payload(item("task", "разобрать балкон", confidence=0.4)))

    attempt = start(llm, "ну надо бы балкон когда-нибудь")

    assert attempt.state is FlowState.awaiting_explicit_confirmation
    assert attempt.issue is ParseIssue.low_confidence
    assert attempt.saveable_items == []
    text = parse_flow.confirmation_text(attempt)
    assert "Не уверен" in text, "бот не изображает уверенность, которой нет"
    # Подтвердить всё равно можно — решает человек, а не порог.
    assert parse_flow.confirm(attempt).saveable_items


def test_битый_ответ_модели_не_сохраняется_и_говорит_об_этом_иначе():
    llm = FakeLLM(payload(item("event", "созвон", date=TOMORROW, time=None)))

    attempt = start(llm, "созвон завтра")

    assert attempt.state is FlowState.failed
    assert attempt.issue is ParseIssue.invalid_model_response
    assert attempt.saveable_items == []
    assert "Не смог разобрать" in parse_flow.confirmation_text(attempt)


def test_провайдер_лёг_можно_переформулировать():
    llm = FakeLLM(error=LLMError("timeout"))

    attempt = start(llm, "напомни завтра позвонить")
    assert attempt.state is FlowState.failed

    # Из failed разрешено попросить правку — это шанс переформулировать.
    waiting = parse_flow.request_correction(attempt)
    assert waiting.state is FlowState.awaiting_correction


def test_нельзя_подтвердить_дважды_и_править_невовремя():
    llm = FakeLLM(payload(item("task", "купить корм", date=TOMORROW)))
    attempt = start(llm, "завтра купить корм")

    with pytest.raises(IllegalTransition):
        correct(attempt, llm, "не корм, а наполнитель")  # правку ждут только после [Исправить]

    confirmed = parse_flow.confirm(attempt)
    with pytest.raises(IllegalTransition):
        parse_flow.confirm(confirmed)
    with pytest.raises(IllegalTransition):
        parse_flow.discard(confirmed)


def test_после_трёх_правок_бот_перестаёт_мучить():
    llm = FakeLLM(*[payload(item("task", f"попытка {n}", date=TOMORROW)) for n in range(5)])
    attempt = start(llm, "что-то невнятное")

    for n in range(parse_flow.MAX_CORRECTION_ROUNDS):
        attempt = correct(parse_flow.request_correction(attempt), llm, f"да не так, вот так {n}")

    attempt = correct(parse_flow.request_correction(attempt), llm, "снова не так")
    assert attempt.state is FlowState.failed
    assert "правок" in attempt.detail


def test_сервисный_слой_не_знает_про_telegram():
    """
    Требование шага: чистая бизнес-логика. Проверяем не словами, а импортами —
    в /services не должно быть ни aiogram, ни telegram.
    """
    services_dir = pathlib.Path(__file__).resolve().parents[1] / "services"
    offenders = []
    for path in services_dir.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")) and ("aiogram" in stripped or "telegram" in stripped):
                offenders.append(f"{path.name}: {stripped}")
    assert offenders == [], f"сервисный слой полез в Telegram: {offenders}"


def test_недоступный_провайдер_не_советует_переформулировать():
    """
    Поймано на живом боте: Gemini отвечал "User location is not supported",
    а бот предлагал "скажите иначе" — совет, который не может сработать.
    """
    llm = FakeLLM(error=LLMError("User location is not supported for the API use"))
    attempt = start(llm, "во вторник к врачу")

    assert attempt.state is FlowState.failed
    assert attempt.issue is ParseIssue.provider_unavailable

    text = parse_flow.confirmation_text(attempt)
    assert "не отвечает" in text
    assert "Скажите иначе" not in text, "нельзя предлагать переформулировать то, что не дошло до модели"


def test_непонятое_сообщение_по_прежнему_просит_переформулировать():
    """Обратная сторона: когда модель ответила, но разобрать нечего, совет уместен."""
    llm = FakeLLM(payload())
    attempt = start(llm, "ммм")

    assert attempt.state is FlowState.failed
    assert "Скажите иначе" in parse_flow.confirmation_text(attempt)
