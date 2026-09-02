"""
Пользователи: единственное место, где решается, что делать с новым telegram_id.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import User

# Пока в проекте нет ни одного способа узнать реальный часовой пояс
# пользователя (нет онбординга, нет команды /timezone) — используем дефолт
# схемы (см. db/models.py) и явно фиксируем это здесь как временное решение,
# а не забытый недочёт. Первый шаг, который стоит сделать до реального
# использования кем-то не из МСК: спросить пояс при первом сообщении.
DEFAULT_TIMEZONE = "Europe/Moscow"


async def get_or_create_user(session: AsyncSession, telegram_id: int) -> User:
    """Найти пользователя по telegram_id или завести нового с поясом по умолчанию."""
    user = (await session.scalars(select(User).where(User.telegram_id == telegram_id))).one_or_none()
    if user is not None:
        return user
    user = User(telegram_id=telegram_id, timezone=DEFAULT_TIMEZONE)
    session.add(user)
    await session.flush()  # получить user.id до коммита, он нужен для Item.user_id
    return user
