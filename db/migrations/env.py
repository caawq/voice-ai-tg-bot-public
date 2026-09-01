"""
Окружение Alembic.

Схема меняется миграциями, а не руками: create_all в проекте нет и не будет —
живой проект должен уметь ехать вперёд и откатываться назад предсказуемо.
"""

import asyncio
import pathlib
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

# Корень проекта в sys.path — чтобы работали импорты config/db при любом cwd.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from config import require_database_url  # noqa: E402
from db.base import Base  # noqa: E402
from db import models  # noqa: E402,F401  - регистрирует таблицы в Base.metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Сгенерировать SQL без подключения к БД (alembic upgrade head --sql)."""
    context.configure(
        url=require_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(require_database_url(), poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
