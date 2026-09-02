"""
Часовой планировщик: вечерний чек-ин (Промпт 4) и картинка недели (Промпт 5)
на одном и том же будильнике.

Не отдельный процесс или контейнер, а фоновая корутина внутри самого бота
(запускается из bot/main.py рядом с polling'ом) — для личного бота с одной
инстанцией городить отдельный cron-сервис поверх docker-compose ради пары
часовых задач было бы лишней инфраструктурой.

Общая причина проверять раз в час, а не слать по расписанию на конкретный
момент, — у каждого пользователя свой часовой пояс (users.timezone), и
нужный локальный момент (20:00 для чек-ина, утро понедельника для недели)
наступает у всех в разное время UTC. Один будильник на каждую границу часа
UTC + фильтр "сейчас ли этот момент по поясу пользователя" (services/checkin.py,
services/week_schedule.py) кладёт обе рассылки ровно в нужный локальный час
каждому — без отдельного таймера на пользователя и без двух почти
одинаковых фоновых циклов.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from aiogram import Bot

from bot.handlers.checkin import run_checkin_broadcast
from bot.handlers.week import run_weekly_broadcast

logger = logging.getLogger(__name__)


def _seconds_until_next_hour(now: dt.datetime) -> float:
    next_hour = now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
    return (next_hour - now).total_seconds()


async def hourly_loop(bot: Bot) -> None:
    """
    Раз в час, по границе часа UTC, прогоняет обе рассылки.

    Каждая — в своём try/except: сбой одной (например, картинки недели из-за
    упавшего Chromium) не должен пропустить чек-ин в тот же час, и наоборот.
    Живёт всё время работы бота как фоновая задача (см. bot/main.py) —
    отменяется вместе с остановкой polling'а.
    """
    while True:
        await asyncio.sleep(_seconds_until_next_hour(dt.datetime.now(tz=dt.timezone.utc)))

        try:
            sent = await run_checkin_broadcast(bot)
            if sent:
                logger.info("Вечерний чек-ин: разослано %s пользователям", sent)
        except Exception:
            logger.exception("Ошибка в часовом прогоне вечернего чек-ина")

        try:
            sent = await run_weekly_broadcast(bot)
            if sent:
                logger.info("Картинка недели: разослана %s пользователям", sent)
        except Exception:
            logger.exception("Ошибка в часовом прогоне картинки недели")
