"""Базовый класс всех моделей. Отдельным файлом — чтобы не ловить циклические импорты."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Общий Declarative-базис проекта."""
