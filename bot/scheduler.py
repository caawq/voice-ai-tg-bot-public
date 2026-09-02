"""
Часовой планировщик вечернего чек-ина.

Не отдельный процесс или контейнер, а фоновая корутина внутри самого бота
(запускается из bot/main.py рядом с polling'ом) — для личного бота с одной
инстанцией городить отдельный cron-сервис поверх docker-compose ради одной
часовой задачи было бы лишней инфраструктурой.

Почему "проверять каждый час", а не слать по расписанию на конкретный момент:
у каждого пользователя свой часовой пояс (users.timezone), и 20:00 наступает
у всех в разное время UTC. Общий будильник на каждую границу часа UTC +
фильтр "у кого сейчас 20:00 по его поясу" (services/checkin.py) кладёт
чек-ин ровно в нужный локальный час каждому, без отдельного таймера на
пользователя.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from aiogram import Bot

from bot.handlers.checkin import run_checkin_broadcast

logger = logging.getLogger(__name__)


def _seconds_until_next_hour(now: dt.datetime) -> float:
    next_hour = now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
    return (next_hour - now).total_seconds()


async def hourly_checkin_loop(bot: Bot) -> None:
    """
    Раз в час, по границе часа UTC, прогоняет рассылку чек-ина.

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
            # Один неудачный час не должен насовсем убивать планировщик —
            # следующая попытка будет через час, ошибка идёт в лог, а не
            # теряется молча и не роняет весь процесс бота.
            logger.exception("Ошибка в часовом прогоне вечернего чек-ина")
