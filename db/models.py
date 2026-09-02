"""
Модели данных бота.

Схема держится на двух решениях, и оба стоит держать в голове при любой правке:

1. **Всё время в БД — UTC.** Ни одна колонка не хранит локальное время
   пользователя. "Сегодня", "вечер" и "неделя" считаются из ``users.timezone``
   на чтении — см. ``services/timeframe.py``. Сервер может стоять где угодно,
   пользователь может переехать в другой часовой пояс — данные не протухают.

2. **Производные состояния не хранятся.** У статуса нет значения "overdue":
   задача просрочена, если ``type='task' AND status='pending' AND due_date <
   сегодня`` по таймзоне пользователя. Это вычисляется на чтении
   (``services/items.py``), поэтому рассинхрону между БД и реальностью взяться
   неоткуда и не нужен cron, который в полночь перекрашивает строки.

Три типа записей живут в одной таблице ``items``, потому что читаются они почти
всегда вместе (недельная картинка — это один запрос), а различий в полях мало.
Чтобы "одна таблица" не превратилась в свалку, форма каждого типа закреплена
CHECK-констрейнтами: событию нужен точный момент, задаче — максимум дата, цели —
процент прогресса.
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base


class ItemType(str, enum.Enum):
    """Тип записи. Определяется ИИ по смыслу голосового и дальше не меняется молча."""

    event = "event"  # жёстко привязано ко времени: встреча, звонок, дедлайн с часом
    task = "task"  # надо сделать; максимум привязано к дню, может быть вообще без даты
    goal = "goal"  # долгосрочная цель с процентом прогресса, без даты выполнения


class ItemStatus(str, enum.Enum):
    """
    Статус записи.

    Значения "overdue" здесь нет сознательно — см. модуль-docstring.
    ``deleted`` — мягкое удаление: пользователь мог удалить голосом, а исходный
    транскрипт стоит сохранить, чтобы можно было вернуть.
    """

    pending = "pending"
    done = "done"
    deleted = "deleted"


class User(Base):
    """Пользователь бота. Один Telegram-аккаунт — одна строка."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)

    # IANA-строка вида "Europe/Moscow". Обязательное поле: без него нельзя
    # посчитать ни "сегодня", ни границы недели. Дефолт — стартовая аудитория
    # концепта (русскоязычная); реальный пояс уточняется у пользователя и
    # проверяется через zoneinfo (services/timeframe.validate_timezone).
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=text("'Europe/Moscow'")
    )

    # Тема картинки недели (Промпт 5): бот не может надёжно узнать тему
    # Telegram-клиента, поэтому для MVP всегда light, а пользователь может
    # явно переключить командой /theme dark|light — выбор запоминается сюда.
    theme: Mapped[str] = mapped_column(String(5), nullable=False, server_default=text("'light'"))

    # Час вечернего чек-ина по локальному времени пользователя (Промпт 6,
    # /settings). Раньше был константой в коде (services/checkin.CHECKIN_HOUR),
    # теперь это настройка: 20 остаётся дефолтом схемы, но пользователь может
    # выбрать другой час.
    checkin_hour: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("20"))

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    items: Mapped[list["Item"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
        foreign_keys="Item.user_id",
    )

    __table_args__ = (
        CheckConstraint("theme IN ('light', 'dark')", name="ck_users_theme_valid"),
        CheckConstraint("checkin_hour BETWEEN 0 AND 23", name="ck_users_checkin_hour_range"),
    )

    def __repr__(self) -> str:  # pragma: no cover - удобство отладки
        return f"<User id={self.id} tg={self.telegram_id} tz={self.timezone!r}>"


class Item(Base):
    """
    Запись пользователя: событие, задача или цель.

    Какие поля обязательны, зависит от ``type`` — это закреплено CHECK-ами ниже,
    чтобы кривая запись не проехала мимо схемы даже при баге в коде.
    """

    __tablename__ = "items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    type: Mapped[ItemType] = mapped_column(Enum(ItemType, name="item_type"), nullable=False)
    status: Mapped[ItemStatus] = mapped_column(
        Enum(ItemStatus, name="item_status"), nullable=False, server_default=text("'pending'")
    )

    # Суть записи в том виде, в каком её показывает бот и картинка недели.
    title: Mapped[str] = mapped_column(Text, nullable=False)

    # Только для event: точный момент в UTC.
    start_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    # Только для task: день по таймзоне пользователя. NULL — задача "когда-нибудь",
    # она не попадает ни в просрочку, ни в вечерний чек-ин по дате.
    due_date: Mapped[dt.date | None] = mapped_column(Date)

    # Только для goal: 0..100.
    progress_percent: Mapped[int | None] = mapped_column(SmallInteger)

    # Только для task и только по желанию: задача может относиться к цели, а может
    # не относиться ни к чему. Цель удалили — задача остаётся, просто без цели.
    parent_goal_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("items.id", ondelete="SET NULL")
    )

    # Исходное голосовое: текст распознавания и file_id самого аудио в Telegram.
    # Нужны, чтобы пользователь мог переслушать оригинал, а не только прочитать,
    # как его понял ИИ (принцип "контекст сохраняется" из концепта).
    source_transcript: Mapped[str | None] = mapped_column(Text)
    voice_file_id: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="items", foreign_keys=[user_id])
    parent_goal: Mapped["Item | None"] = relationship(remote_side=[id], foreign_keys=[parent_goal_id])

    __table_args__ = (
        # --- форма записи по типам -------------------------------------------------
        CheckConstraint(
            "type <> 'event' OR (start_at IS NOT NULL AND due_date IS NULL "
            "AND progress_percent IS NULL AND parent_goal_id IS NULL)",
            name="ck_items_event_shape",
        ),
        CheckConstraint(
            "type <> 'task' OR (start_at IS NULL AND progress_percent IS NULL)",
            name="ck_items_task_shape",
        ),
        CheckConstraint(
            "type <> 'goal' OR (start_at IS NULL AND due_date IS NULL "
            "AND parent_goal_id IS NULL AND progress_percent IS NOT NULL)",
            name="ck_items_goal_shape",
        ),
        CheckConstraint(
            "progress_percent IS NULL OR progress_percent BETWEEN 0 AND 100",
            name="ck_items_progress_range",
        ),
        CheckConstraint("parent_goal_id IS NULL OR parent_goal_id <> id", name="ck_items_goal_not_self"),
        CheckConstraint("length(btrim(title)) > 0", name="ck_items_title_not_blank"),
        # --- горячий запрос №1: вся неделя пользователя (картинка хронологии) ------
        # События ищутся по моменту в UTC, задачи — по дню; отсюда два индекса,
        # оба частичные, потому что удалённые записи в картинку не попадают
        # никогда, а хранить их в индексе смысла нет.
        Index(
            "ix_items_user_start_at",
            "user_id",
            "start_at",
            postgresql_where=text("start_at IS NOT NULL AND status <> 'deleted'"),
        ),
        Index(
            "ix_items_user_due_date",
            "user_id",
            "due_date",
            postgresql_where=text("due_date IS NOT NULL AND status <> 'deleted'"),
        ),
        # Цели идут в картинке фоновой полосой прогресса — их мало, но берутся они
        # тем же запросом, поэтому пусть будет свой маленький частичный индекс.
        Index(
            "ix_items_user_goals",
            "user_id",
            postgresql_where=text("type = 'goal' AND status <> 'deleted'"),
        ),
        # --- горячий запрос №2: pending-задачи на сегодня (вечерний чек-ин) --------
        # Этот же индекс обслуживает и просрочку (due_date < сегодня): условие по
        # type и status зашито в предикат, поэтому индекс маленький и попадает в
        # кэш целиком.
        Index(
            "ix_items_user_pending_tasks",
            "user_id",
            "due_date",
            postgresql_where=text("type = 'task' AND status = 'pending'"),
        ),
        # Обратный поиск "задачи этой цели" и дешёвый ON DELETE SET NULL.
        Index(
            "ix_items_parent_goal_id",
            "parent_goal_id",
            postgresql_where=text("parent_goal_id IS NOT NULL"),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - удобство отладки
        return f"<Item id={self.id} {self.type.value}/{self.status.value} {self.title[:30]!r}>"
