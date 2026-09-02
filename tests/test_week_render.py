"""
services/week_render.py и services/week_schedule.py — чистая логика, без БД
и без Telegram: маппинг items в структуру week_card.html.jinja и выбор
адресатов еженедельной рассылки по локальному времени.
"""

from __future__ import annotations

import datetime as dt

from db.models import Item, ItemStatus, ItemType
from services import timeframe, week_render, week_schedule

MOSCOW = "Europe/Moscow"


def _event(title: str, local_dt: dt.datetime, timezone: str = MOSCOW) -> Item:
    return Item(type=ItemType.event, title=title, start_at=timeframe.to_utc(local_dt, timezone))


def _task(title: str, due: dt.date, status: ItemStatus = ItemStatus.pending) -> Item:
    return Item(type=ItemType.task, title=title, due_date=due, status=status)


def _goal(title: str, percent: int, updated_at: dt.datetime) -> Item:
    return Item(type=ItemType.goal, title=title, progress_percent=percent, updated_at=updated_at)


# --- date_range -----------------------------------------------------------------


def test_date_range_в_пределах_одного_месяца():
    monday = dt.date(2026, 8, 24)
    sunday = monday + dt.timedelta(days=6)
    assert week_render._format_date_range(monday, sunday) == "24–30 августа"


def test_date_range_на_стыке_месяцев():
    monday = dt.date(2026, 8, 31)  # понедельник
    sunday = monday + dt.timedelta(days=6)
    assert week_render._format_date_range(monday, sunday) == "31 августа – 6 сентября"


def test_date_range_на_стыке_годов_упоминает_оба_года():
    monday = timeframe.week_start(dt.date(2026, 12, 30))
    sunday = monday + dt.timedelta(days=6)
    if monday.year == sunday.year:
        return  # эта конкретная неделя не на стыке — тест про другое поведение не проверяет
    result = week_render._format_date_range(monday, sunday)
    assert str(monday.year) in result
    assert str(sunday.year) in result


# --- дни: события/задачи/просрочка -----------------------------------------------


def test_событие_попадает_в_свой_локальный_день_со_временем():
    monday = dt.date(2026, 8, 24)
    today = monday
    items = [_event("Созвон", dt.datetime(2026, 8, 25, 10, 0))]
    data = week_render.build_week_data(items, monday, MOSCOW, today)
    tuesday = data["days"][1]
    assert tuesday["items"] == [{"type": "event", "time": "10:00", "label": "Созвон"}]
    assert data["days"][0]["items"] == []


def test_просроченная_pending_задача_помечается_overdue_а_выполненная_нет():
    monday = dt.date(2026, 8, 24)
    today = dt.date(2026, 8, 26)  # среда той же недели
    items = [
        _task("Написать отчёт", due=dt.date(2026, 8, 24)),  # понедельник, всё ещё pending -> overdue
        _task("Оплатить интернет", due=dt.date(2026, 8, 24), status=ItemStatus.done),  # done -> не overdue
        _task("Купить корм", due=dt.date(2026, 8, 26)),  # сегодня, срок ещё не прошёл -> просто task
    ]
    data = week_render.build_week_data(items, monday, MOSCOW, today)

    monday_items = {i["label"]: i["type"] for i in data["days"][0]["items"]}
    assert monday_items["Написать отчёт"] == "overdue"
    assert monday_items["Оплатить интернет"] == "task_done"

    wednesday_items = {i["label"]: i["type"] for i in data["days"][2]["items"]}
    assert wednesday_items["Купить корм"] == "task"


def test_несколько_целей_берётся_последняя_обновлённая():
    monday = dt.date(2026, 8, 24)
    older = _goal("Английский", 40, dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
    newer = _goal("Спорт", 10, dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc))
    data = week_render.build_week_data([older, newer], monday, MOSCOW, monday)
    assert data["goal"] == {"label": "Спорт", "percent": 10}


def test_без_целей_goal_равен_none():
    monday = dt.date(2026, 8, 24)
    data = week_render.build_week_data([], monday, MOSCOW, monday)
    assert data["goal"] is None
    assert len(data["days"]) == 7
    assert [d["name"] for d in data["days"]] == ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


# --- services/week_schedule.py ----------------------------------------------------


def test_is_weekly_send_time_понедельник_утро_по_поясу():
    # 5:00 UTC = 8:00 в Москве (UTC+3) в понедельник 2026-08-31.
    at_utc = dt.datetime(2026, 8, 31, 5, 0, tzinfo=dt.timezone.utc)
    assert week_schedule.is_weekly_send_time(MOSCOW, at_utc) is True


def test_is_weekly_send_time_не_срабатывает_в_другой_день_или_час():
    tuesday_8am_utc = dt.datetime(2026, 9, 1, 5, 0, tzinfo=dt.timezone.utc)  # вторник, тот же час
    assert week_schedule.is_weekly_send_time(MOSCOW, tuesday_8am_utc) is False

    monday_other_hour = dt.datetime(2026, 8, 31, 10, 0, tzinfo=dt.timezone.utc)  # понедельник, не тот час
    assert week_schedule.is_weekly_send_time(MOSCOW, monday_other_hour) is False
