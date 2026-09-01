"""
Структурирование расшифрованного голосового в записи бота.

Чистая бизнес-логика: на вход — текст расшифровки, на выход — список записей.
Про Telegram модуль не знает ничего (ни Message, ни chat_id, ни кнопок), про
базу — тоже: он ничего не сохраняет. Поэтому его можно гонять тестами без
бота, без сети и без БД, подсунув фейковый LLM-клиент.

Что здесь важного:

* **Одно голосовое — это массив записей.** "Напомни позвонить клиенту и купи
  корм коту" — две разные записи, а не одна строка с "и". Разбивает их модель,
  мы только валидируем результат.
* **Структуру даёт нативный механизм провайдера** (services/llm.py), а не
  просьба "верни JSON". Но даже strict-схема гарантирует лишь форму, а не
  смысл: модель может прислать событие без времени или процент 150. Поэтому
  после провайдера идёт своя валидация — и всё, что её не прошло, не
  сохраняется молча.
* **Ничего не сохраняется автоматически при сомнении.** Низкая уверенность или
  битый ответ — это не повод угадать, это повод переспросить явно
  (см. services/parse_flow.py).
"""

from __future__ import annotations

import datetime as dt
import enum
from dataclasses import dataclass, field
from typing import Any

from db.models import ItemType
from services.llm import LLMClient, LLMError

# Порог доверия. Ниже него бот не показывает бодрое "Записал: ...", а
# переспрашивает явной формулировкой. 0.75 — сознательно строго: цена ошибки
# (человек не пришёл на встречу, потому что бот записал не то) выше, чем цена
# лишнего вопроса.
CONFIDENCE_THRESHOLD = 0.75

# Максимум записей из одного голосового: защита от того, что модель нарежет
# длинный монолог на десятки мелких пунктов и завалит человека подтверждениями.
MAX_ITEMS_PER_MESSAGE = 10

SCHEMA_NAME = "save_items"

# Схема в strict-совместимом виде: все поля обязательны, необязательность
# выражена типом ["string", "null"], лишние ключи запрещены.
ITEMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "label", "date", "time", "goal_progress", "confidence_score"],
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["event", "task", "goal"],
                        "description": (
                            "event — привязано к конкретному времени (встреча, звонок в 15:00); "
                            "task — надо сделать, максимум привязано ко дню; "
                            "goal — долгосрочная цель без даты выполнения"
                        ),
                    },
                    "label": {
                        "type": "string",
                        "description": "Суть в 2-5 словах, словами пользователя, без слова 'напомни'",
                    },
                    "date": {
                        "type": ["string", "null"],
                        "description": "YYYY-MM-DD. null, если даты нет (цель или задача 'когда-нибудь')",
                    },
                    "time": {
                        "type": ["string", "null"],
                        "description": "HH:MM по местному времени пользователя. null, если времени нет",
                    },
                    "goal_progress": {
                        "type": ["integer", "null"],
                        "description": "0-100, только для type=goal; для остальных null",
                    },
                    "confidence_score": {
                        "type": "number",
                        "description": "0..1 — насколько уверен в типе, сути и времени вместе",
                    },
                },
            },
        }
    },
}

_WEEKDAYS_RU = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]

SYSTEM_PROMPT = """Ты разбираешь расшифровки голосовых сообщений в задачи, события и цели.

Сегодня {today} ({weekday}), время пользователя — часовой пояс {timezone}.
Все даты и время считай относительно этого, а не относительно чего-то ещё.

Типы записей:
- event — привязано к конкретному часу: встреча, звонок, тренировка в 18:30.
  У события ОБЯЗАТЕЛЬНО есть и дата, и время.
- task — надо сделать, часа нет. Дата может быть ("завтра купить корм"), а
  может не быть вовсе ("когда-нибудь разобрать балкон") — тогда date = null.
- goal — долгосрочная цель без даты выполнения: "подтянуть английский за
  месяц". date и time = null, goal_progress = 0, если пользователь не сказал,
  что уже что-то сделал.

Правила:
1. В одном сообщении может быть несколько дел — верни их отдельными
   элементами. "Напомни позвонить клиенту и купи корм" — это две записи.
2. Относительные даты переводи в абсолютные: "завтра", "в пятницу", "через
   неделю" — в YYYY-MM-DD. Если названо время суток без часа ("вечером"),
   ставь разумный час (утро 09:00, день 14:00, вечер 19:00) и снижай
   confidence_score — ты угадал, а не услышал.
3. label — суть в 2-5 словах словами пользователя. Без "напомни", без
   "надо бы", без пересказа.
4. confidence_score честный: 0.9+ — всё названо прямо; 0.5-0.75 — что-то
   додумал (тип, час, дату); ниже 0.5 — не уверен даже в сути.
5. Ничего не выдумывай: если из фразы не следует дело, верни пустой items."""

CORRECTION_PROMPT = """Пользователь уточняет ПРЕДЫДУЩИЙ разбор, а не диктует новое дело.

Возьми записи из прошлой попытки и применить к ним правку. Что пользователь не
трогал — оставь как было, включая даты и время. Если правка добавляет новое
дело — добавь его к списку, а не заменяй им остальные. Верни ПОЛНЫЙ итоговый
список записей, а не только изменённые."""


class ParseIssue(str, enum.Enum):
    """Почему результат нельзя сохранять молча."""

    none = "none"  # всё чисто, можно показывать "Записал: ... [Да] [Исправить]"
    nothing_recognized = "nothing_recognized"  # модель не нашла ни одного дела
    low_confidence = "low_confidence"  # разобрал, но не уверен — спрашиваем явно
    invalid_model_response = "invalid_model_response"  # схема прошла, смысл — нет
    provider_unavailable = "provider_unavailable"  # сеть, лимиты, неверный ключ


@dataclass(frozen=True, slots=True)
class ParsedItem:
    """Одна запись, как её понял ИИ. В БД ещё не сохранена и может не сохраниться."""

    type: ItemType
    label: str
    date: dt.date | None
    time: dt.time | None
    goal_progress: int | None
    confidence_score: float

    def as_dict(self) -> dict[str, Any]:
        """Обратно в JSON-вид — чтобы отдать модели как контекст прошлой попытки."""
        return {
            "type": self.type.value,
            "label": self.label,
            "date": self.date.isoformat() if self.date else None,
            "time": self.time.strftime("%H:%M") if self.time else None,
            "goal_progress": self.goal_progress,
            "confidence_score": round(self.confidence_score, 2),
        }


@dataclass(frozen=True, slots=True)
class ParseResult:
    """Итог одного вызова модели."""

    items: list[ParsedItem] = field(default_factory=list)
    issue: ParseIssue = ParseIssue.none
    detail: str = ""

    @property
    def is_clean(self) -> bool:
        """Можно показывать обычное подтверждение, без усиленной формулировки."""
        return self.issue is ParseIssue.none

    @property
    def min_confidence(self) -> float:
        """Уверенность результата = уверенность самой слабой записи в нём."""
        return min((i.confidence_score for i in self.items), default=0.0)


def build_messages(
    transcript: str,
    *,
    previous_items: list[ParsedItem] | None = None,
    correction: str | None = None,
) -> list[dict[str, str]]:
    """
    Собрать диалог для модели.

    Обычный разбор — одно сообщение пользователя. Правка — три: исходная фраза,
    прошлый разбор от лица ассистента и текст правки. Именно из-за третьего
    случая модель понимает, что это уточнение, а не новая независимая запись.
    """
    import json

    messages: list[dict[str, str]] = [{"role": "user", "content": transcript}]
    if previous_items is not None and correction is not None:
        messages.append(
            {
                "role": "assistant",
                "content": json.dumps(
                    {"items": [i.as_dict() for i in previous_items]}, ensure_ascii=False
                ),
            }
        )
        messages.append({"role": "user", "content": f"{CORRECTION_PROMPT}\n\nПравка: {correction}"})
    return messages


def _validate(payload: dict[str, Any]) -> tuple[list[ParsedItem], str]:
    """
    Превратить ответ модели в записи и объяснить, что не так, если не вышло.

    Схема провайдера гарантирует форму, эта функция — смысл: событию нужен час,
    у цели не бывает даты, процент лежит в 0..100. Возвращает (записи, ошибка);
    непустая ошибка означает, что доверять ответу нельзя целиком.
    """
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        return [], "в ответе нет списка items"
    if len(raw_items) > MAX_ITEMS_PER_MESSAGE:
        return [], f"слишком много записей за раз: {len(raw_items)}"

    parsed: list[ParsedItem] = []
    for index, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            return [], f"запись {index} не объект"

        try:
            item_type = ItemType(raw.get("type"))
        except ValueError:
            return [], f"запись {index}: неизвестный тип {raw.get('type')!r}"

        label = str(raw.get("label") or "").strip()
        if not label:
            return [], f"запись {index}: пустой заголовок"

        date_value: dt.date | None = None
        if raw.get("date"):
            try:
                date_value = dt.date.fromisoformat(str(raw["date"]))
            except ValueError:
                return [], f"запись {index}: дата {raw['date']!r} не в формате YYYY-MM-DD"

        time_value: dt.time | None = None
        if raw.get("time"):
            try:
                time_value = dt.datetime.strptime(str(raw["time"]), "%H:%M").time()
            except ValueError:
                return [], f"запись {index}: время {raw['time']!r} не в формате HH:MM"

        confidence = raw.get("confidence_score")
        if not isinstance(confidence, (int, float)) or not 0.0 <= float(confidence) <= 1.0:
            return [], f"запись {index}: confidence_score вне диапазона 0..1"

        progress = raw.get("goal_progress")

        if item_type is ItemType.event:
            # Событие без часа — это не событие, а задача на день. Но решать за
            # пользователя, что он имел в виду, нельзя: возвращаем ошибку и
            # переспрашиваем.
            if date_value is None or time_value is None:
                return [], f"запись {index}: событие без даты или времени"
            progress = None
        elif item_type is ItemType.task:
            # Модель нередко отдаёт задачу с часом ("купить корм в 18:00").
            # По схеме проекта запись с точным часом — это событие; повышаем
            # тип, а не выбрасываем время. Нормализация, а не догадка о смысле.
            if time_value is not None:
                if date_value is None:
                    return [], f"запись {index}: время без даты"
                item_type = ItemType.event
            progress = None
        else:  # goal
            if progress is None:
                progress = 0
            if not isinstance(progress, int) or not 0 <= progress <= 100:
                return [], f"запись {index}: goal_progress вне диапазона 0..100"
            # У цели нет срока по определению — дату и время игнорируем молча,
            # это не ошибка модели, а лишняя вежливость с её стороны.
            date_value = None
            time_value = None

        parsed.append(
            ParsedItem(
                type=item_type,
                label=label,
                date=date_value,
                time=time_value,
                goal_progress=progress,
                confidence_score=float(confidence),
            )
        )

    return parsed, ""


async def parse_transcript(
    transcript: str,
    *,
    client: LLMClient,
    today: dt.date,
    timezone: str,
    previous_items: list[ParsedItem] | None = None,
    correction: str | None = None,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
) -> ParseResult:
    """
    Разобрать расшифровку голосового в записи.

    today и timezone приходят снаружи (из users.timezone, см.
    services/timeframe.py) — модуль не спрашивает время у системы, иначе
    "завтра" считалось бы по часам сервера.

    previous_items + correction — режим правки: см. build_messages и
    services/parse_flow.py.

    Никогда не бросает исключений наружу: любая беда превращается в ParseResult
    с issue, потому что вызывающему коду в любом случае надо что-то сказать
    человеку, а не упасть.
    """
    text = transcript.strip()
    if not text:
        return ParseResult(issue=ParseIssue.nothing_recognized, detail="пустая расшифровка")

    system = SYSTEM_PROMPT.format(
        today=today.isoformat(), weekday=_WEEKDAYS_RU[today.weekday()], timezone=timezone
    )
    messages = build_messages(text, previous_items=previous_items, correction=correction)

    try:
        payload = await client.structured_call(
            system=system, messages=messages, schema=ITEMS_SCHEMA, schema_name=SCHEMA_NAME
        )
    except LLMError as exc:
        return ParseResult(issue=ParseIssue.provider_unavailable, detail=str(exc))

    items, error = _validate(payload)
    if error:
        return ParseResult(issue=ParseIssue.invalid_model_response, detail=error)
    if not items:
        return ParseResult(issue=ParseIssue.nothing_recognized, detail="модель не нашла ни одного дела")

    result = ParseResult(items=items)
    if result.min_confidence < confidence_threshold:
        return ParseResult(
            items=items,
            issue=ParseIssue.low_confidence,
            detail=f"минимальная уверенность {result.min_confidence:.2f} < {confidence_threshold}",
        )
    return result


if __name__ == "__main__":  # pragma: no cover
    # Ручная проверка на ЖИВОМ провайдере — то, чего не могут юнит-тесты с фейком.
    #   python -m services.voice_parsing "напомни завтра в 15 позвонить клиенту и купи корм коту"
    # Нужны LLM_API_KEY, LLM_BASE_URL и LLM_MODEL в .env.
    import asyncio
    import sys

    from services.llm import OpenAICompatibleClient
    from services import timeframe

    phrase = " ".join(sys.argv[1:]) or "напомни завтра в 15 позвонить клиенту и купи корм коту"
    tz = "Europe/Moscow"
    outcome = asyncio.run(
        parse_transcript(
            phrase,
            client=OpenAICompatibleClient(),
            today=timeframe.today(tz),
            timezone=tz,
        )
    )
    print(f"issue: {outcome.issue.value} {outcome.detail}".strip())
    for parsed in outcome.items:
        print("  ", parsed.as_dict())
