"""
Маппинг недели пользователя (items из БД) в структуру, которую ждёт
render/render_week.py.

Про рендер и про Telegram здесь ничего нет — только данные. render_week.py
сознательно не знает про БД (см. его docstring), а эта прослойка не знает
про Playwright и про Jinja: связывает их bot/handlers/week.py.

Просрочка вычисляется здесь же, "на лету", тем же правилом, что и везде в
проекте (services.items.is_overdue) — отдельного поля/статуса "overdue" в
БД нет и не будет (см. db/models.py, модуль-docstring).
"""

from __future__ import annotations

import datetime as dt

from db.models import Item, ItemStatus, ItemType
from services import items as items_svc
from services import timeframe

_WEEKDAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_MONTHS_GENITIVE = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def _format_date_range(monday: dt.date, sunday: dt.date) -> str:
    """"11–17 августа", а на стыке месяцев/годов — оба месяца (и годы) явно."""
    if monday.year != sunday.year:
        return (
            f"{monday.day} {_MONTHS_GENITIVE[monday.month - 1]} {monday.year} – "
            f"{sunday.day} {_MONTHS_GENITIVE[sunday.month - 1]} {sunday.year}"
        )
    if monday.month != sunday.month:
        return (
            f"{monday.day} {_MONTHS_GENITIVE[monday.month - 1]} – "
            f"{sunday.day} {_MONTHS_GENITIVE[sunday.month - 1]}"
        )
    return f"{monday.day}–{sunday.day} {_MONTHS_GENITIVE[monday.month - 1]}"


def _pick_active_goal(items: list[Item]) -> Item | None:
    """
    Если активных целей несколько — берём одну, последнюю обновлённую.

    Сознательное ограничение MVP (Промпт 5, п.3): week_card.html.jinja рисует
    одну ленту прогресса, а не несколько, — не пытаемся обойти это на уровне
    данных.
    """
    goals = [i for i in items if i.type is ItemType.goal]
    if not goals:
        return None
    return max(goals, key=lambda g: g.updated_at)


def _day_items(items: list[Item], day: dt.date, timezone: str, today: dt.date) -> list[dict]:
    result: list[dict] = []
    for item in items:
        if item.type is ItemType.event:
            local_start = timeframe.to_local(item.start_at, timezone)
            if local_start.date() != day:
                continue
            result.append({"type": "event", "time": local_start.strftime("%H:%M"), "label": item.title})
        elif item.type is ItemType.task:
            if item.due_date != day:
                continue
            if item.status == ItemStatus.done:
                result.append({"type": "task_done", "label": item.title})
            elif items_svc.is_overdue(item, today):
                result.append({"type": "overdue", "label": item.title})
            else:
                result.append({"type": "task", "label": item.title})
        # goal в дни не попадает — он одна лента прогресса на всю неделю, см. _pick_active_goal

    # События — по времени первыми, остальное следом в исходном порядке
    # запроса (week_items_stmt уже отдаёт due_date/id по возрастанию).
    result.sort(key=lambda entry: (entry["type"] != "event", entry.get("time", "")))
    return result


def build_week_data(items: list[Item], monday: dt.date, timezone: str, today: dt.date) -> dict:
    """
    items — результат services.items.week_items(session, user, monday) (уже
    отфильтрован по неделе и по видимости). monday/today — по локальному
    времени пользователя (services.timeframe), не по времени сервера.
    """
    sunday = monday + dt.timedelta(days=6)
    days = [
        {
            "name": _WEEKDAY_NAMES[offset],
            "num": (monday + dt.timedelta(days=offset)).day,
            "items": _day_items(items, monday + dt.timedelta(days=offset), timezone, today),
        }
        for offset in range(7)
    ]

    goal_item = _pick_active_goal(items)
    goal = {"label": goal_item.title, "percent": goal_item.progress_percent} if goal_item else None

    return {"date_range": _format_date_range(monday, sunday), "goal": goal, "days": days}
