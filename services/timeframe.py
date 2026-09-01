"""
Локальное время пользователя.

В БД всё лежит в UTC, а человек живёт в своём поясе: его "сегодня" начинается
в его полночь, а не в полночь сервера. Весь перевод одного в другое собран
здесь — чтобы ни один запрос и ни один хендлер не считал даты по времени
машины, на которой случайно запустился процесс.

Про Telegram здесь ничего нет и быть не должно: это чистые вычисления над
датами, их легко проверить без бота и без базы.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = dt.timezone.utc


def validate_timezone(name: str) -> str:
    """
    Проверить IANA-строку и вернуть её же.

    Вызывается при сохранении пояса пользователя: лучше отказать сразу, чем
    обнаружить мусор в поле в момент вечернего чек-ина.
    """
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Неизвестная таймзона: {name!r}. Нужна IANA-строка, например 'Europe/Moscow'.") from exc
    return name


def tz_of(timezone: str) -> ZoneInfo:
    """ZoneInfo по строке из users.timezone."""
    return ZoneInfo(timezone)


def now_local(timezone: str) -> dt.datetime:
    """Текущий момент в поясе пользователя."""
    return dt.datetime.now(tz=tz_of(timezone))


def today(timezone: str) -> dt.date:
    """
    "Сегодня" пользователя.

    Именно от этой даты считается просрочка: задача просрочена, если её
    due_date меньше этого значения. Ни один вызов не должен подставлять сюда
    date.today() сервера.
    """
    return now_local(timezone).date()


def week_start(day: dt.date) -> dt.date:
    """Понедельник недели, в которую попадает day. Неделя в проекте — Пн..Вс."""
    return day - dt.timedelta(days=day.weekday())


def week_days(monday: dt.date) -> list[dt.date]:
    """Семь дат недели, Пн..Вс — ровно то, что рисует картинка хронологии."""
    return [monday + dt.timedelta(days=i) for i in range(7)]


def day_bounds_utc(day: dt.date, timezone: str) -> tuple[dt.datetime, dt.datetime]:
    """
    Границы локальных суток в UTC: [начало дня, начало следующего дня).

    Полуинтервал, а не BETWEEN: событие ровно в полночь принадлежит новому дню
    и только ему.
    """
    tz = tz_of(timezone)
    start_local = dt.datetime.combine(day, dt.time.min, tzinfo=tz)
    end_local = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, tzinfo=tz)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def week_bounds_utc(monday: dt.date, timezone: str) -> tuple[dt.datetime, dt.datetime]:
    """
    Границы недели Пн..Вс в UTC.

    Считается через реальные локальные полуночи, поэтому неделя с переводом
    часов честно получается на час длиннее или короче — а не ровно 168 часов.
    """
    start, _ = day_bounds_utc(monday, timezone)
    _, end = day_bounds_utc(monday + dt.timedelta(days=6), timezone)
    return start, end


def to_local(moment: dt.datetime, timezone: str) -> dt.datetime:
    """UTC-момент из БД -> время пользователя (для показа и для картинки)."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(tz_of(timezone))


def to_utc(moment: dt.datetime, timezone: str) -> dt.datetime:
    """Локальное время пользователя -> UTC (для записи в БД)."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz_of(timezone))
    return moment.astimezone(UTC)
