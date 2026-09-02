"""
Экраны кнопочного интерфейса: тексты и клавиатуры (Промпт 6).

Отдельно от хендлеров, потому что один и тот же экран открывается из разных
мест: карточка записи — из /list, из /today и из клавиатуры под обложкой
недели. Хендлеры отвечают за "что случилось", этот модуль — за "как это
выглядит".

Здесь же живёт edit_view: список и карточка всегда РЕДАКТИРУЮТ существующее
сообщение, а не шлют новое (Промпт 6, п.7). У сообщения с обложкой недели
редактируется подпись, у обычного — текст: это единственное различие, и
пусть оно будет в одном месте.
"""

from __future__ import annotations

import datetime as dt

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.callbacks import SRC_LIST, SRC_TODAY, SRC_WEEK, ListCB, RecordCB, SettingsCB
from db.models import Item, ItemStatus, ItemType, User
from services import items as items_svc
from services import records as records_svc
from services import timeframe

_WEEKDAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_MONTHS_GENITIVE = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]

WEEK_CAPTION = "Планы на неделю. Нажмите на запись, чтобы открыть карточку."


async def edit_view(message: Message, text: str, keyboard: InlineKeyboardMarkup | None) -> None:
    """
    Перерисовать экран на месте. У сообщения с фото (обложка недели) текста
    нет вообще — Telegram разрешает менять только подпись.
    """
    if getattr(message, "photo", None):
        await message.edit_caption(caption=text, reply_markup=keyboard)
    else:
        await message.edit_text(text, reply_markup=keyboard)


def _icon(item: Item, today: dt.date) -> str:
    if item.status == ItemStatus.done:
        return "✅"
    if items_svc.is_overdue(item, today):
        return "⚠️"
    return {ItemType.event: "📅", ItemType.task: "📌", ItemType.goal: "🎯"}[item.type]


def _when_short(item: Item, timezone: str) -> str:
    if item.type is ItemType.event and item.start_at is not None:
        local = timeframe.to_local(item.start_at, timezone)
        return f"{local.day:02d}.{local.month:02d} {local:%H:%M}"
    if item.due_date is not None:
        return f"{item.due_date.day:02d}.{item.due_date.month:02d}"
    return ""


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _row_label(item: Item, today: dt.date, timezone: str) -> str:
    when = _when_short(item, timezone)
    tail = f" · {when}" if when else ""
    return _short(f"{_icon(item, today)} {item.title}{tail}", 60)


# --------------------------------------------------------------------- список


def list_text(src: str, items: list[Item], page: int, total_pages: int, ftype: str, fstat: str) -> str:
    if src == SRC_TODAY:
        head = "Сегодня"
    elif src == SRC_WEEK:
        return WEEK_CAPTION
    else:
        head = (
            f"Все записи · {records_svc.TYPE_LABELS[ftype]} · "
            f"{records_svc.STATUS_LABELS[fstat]}"
        )

    if not items:
        empty = "На сегодня ничего нет." if src == SRC_TODAY else "По этим фильтрам записей нет."
        return f"{head}\n\n{empty}"

    pages = f"\n\nСтраница {page + 1} из {total_pages}" if total_pages > 1 else ""
    return f"{head}{pages}"


def list_keyboard(
    src: str, items: list[Item], page: int, total_pages: int, ftype: str, fstat: str, today: dt.date, timezone: str
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    for item in items:
        rows.append(
            [
                InlineKeyboardButton(
                    text=_row_label(item, today, timezone),
                    callback_data=RecordCB(
                        act="o", id=item.id, src=src, page=page, ftype=ftype, fstat=fstat
                    ).pack(),
                )
            ]
        )

    if total_pages > 1:
        rows.append(_pager_row(src, page, total_pages, ftype, fstat))

    if src == SRC_LIST:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"Тип: {records_svc.TYPE_LABELS[ftype]}",
                    callback_data=ListCB(act="ft", src=src, page=0, ftype=ftype, fstat=fstat).pack(),
                ),
                InlineKeyboardButton(
                    text=f"Статус: {records_svc.STATUS_LABELS[fstat]}",
                    callback_data=ListCB(act="fs", src=src, page=0, ftype=ftype, fstat=fstat).pack(),
                ),
            ]
        )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def _pager_row(src: str, page: int, total_pages: int, ftype: str, fstat: str) -> list[InlineKeyboardButton]:
    prev_page = (page - 1) % total_pages
    next_page = (page + 1) % total_pages
    return [
        InlineKeyboardButton(
            text="◀️",
            callback_data=ListCB(act="p", src=src, page=prev_page, ftype=ftype, fstat=fstat).pack(),
        ),
        InlineKeyboardButton(
            text=f"{page + 1}/{total_pages}",
            callback_data=ListCB(act="n", src=src, page=page, ftype=ftype, fstat=fstat).pack(),
        ),
        InlineKeyboardButton(
            text="▶️",
            callback_data=ListCB(act="p", src=src, page=next_page, ftype=ftype, fstat=fstat).pack(),
        ),
    ]


# ------------------------------------------------- клавиатура под обложкой недели


def week_keyboard(
    items: list[Item], page: int, total_pages: int, today: dt.date, timezone: str
) -> InlineKeyboardMarkup:
    """
    Кнопки-записи под обложкой недели: по одной на запись, в порядке Пн → Вс,
    лейбл — день недели плюс название (Промпт 6, п.5).
    """
    rows: list[list[InlineKeyboardButton]] = []
    for item in items:
        day = _weekday_of(item, timezone)
        prefix = f"{_WEEKDAYS_SHORT[day]} · " if day is not None else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=_short(f"{prefix}{item.title}", 45),
                    callback_data=RecordCB(
                        act="o", id=item.id, src=SRC_WEEK, page=page, ftype="a", fstat="x"
                    ).pack(),
                )
            ]
        )

    if total_pages > 1:
        rows.append(_pager_row(SRC_WEEK, page, total_pages, "a", "x"))

    return InlineKeyboardMarkup(inline_keyboard=rows)


def _weekday_of(item: Item, timezone: str) -> int | None:
    if item.type is ItemType.event and item.start_at is not None:
        return timeframe.to_local(item.start_at, timezone).weekday()
    if item.due_date is not None:
        return item.due_date.weekday()
    return None


def week_sort_key(item: Item, timezone: str) -> tuple:
    """Пн → Вс; записи без дня (цели) уходят в конец."""
    day = _weekday_of(item, timezone)
    return (day is None, day if day is not None else 0, item.id)


# -------------------------------------------------------------------- карточка


def card_text(item: Item, today: dt.date, timezone: str) -> str:
    kind = {ItemType.event: "Событие", ItemType.task: "Задача", ItemType.goal: "Цель"}[item.type]

    lines = [f"{_icon(item, today)} {item.title}", ""]

    if item.type is ItemType.event and item.start_at is not None:
        local = timeframe.to_local(item.start_at, timezone)
        lines.append(f"{kind} · {local.day} {_MONTHS_GENITIVE[local.month - 1]}, {local:%H:%M}")
    elif item.type is ItemType.task:
        when = (
            f"{item.due_date.day} {_MONTHS_GENITIVE[item.due_date.month - 1]}"
            if item.due_date
            else "без даты"
        )
        lines.append(f"{kind} · {when}")
    else:
        lines.append(f"{kind} · прогресс {item.progress_percent or 0}%")

    if item.status == ItemStatus.done:
        lines.append("Статус: выполнено")
    elif items_svc.is_overdue(item, today):
        days = (today - item.due_date).days
        lines.append(f"Статус: просрочено на {days} дн.")
    else:
        lines.append("Статус: активна")

    return "\n".join(lines)


def card_keyboard(item: Item, src: str, page: int, ftype: str, fstat: str) -> InlineKeyboardMarkup:
    def cb(act: str, arg: str = "") -> str:
        return RecordCB(act=act, id=item.id, src=src, page=page, ftype=ftype, fstat=fstat, arg=arg).pack()

    rows: list[list[InlineKeyboardButton]] = []

    actions = []
    if item.status != ItemStatus.done:
        actions.append(InlineKeyboardButton(text="✅ Готово", callback_data=cb("d")))
    if records_svc.can_postpone(item):
        actions.append(InlineKeyboardButton(text="🗓 Перенести", callback_data=cb("pm")))
    if actions:
        rows.append(actions)

    rows.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=cb("da"))])
    rows.append([InlineKeyboardButton(text="◀️ Назад к списку", callback_data=cb("b"))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def postpone_keyboard(item: Item, src: str, page: int, ftype: str, fstat: str) -> InlineKeyboardMarkup:
    def cb(act: str, arg: str = "") -> str:
        return RecordCB(act=act, id=item.id, src=src, page=page, ftype=ftype, fstat=fstat, arg=arg).pack()

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сегодня", callback_data=cb("p", "t")),
                InlineKeyboardButton(text="Завтра", callback_data=cb("p", "m")),
                InlineKeyboardButton(text="+ неделя", callback_data=cb("p", "w")),
            ],
            [InlineKeyboardButton(text="◀️ Отмена", callback_data=cb("o"))],
        ]
    )


def delete_confirm_keyboard(item: Item, src: str, page: int, ftype: str, fstat: str) -> InlineKeyboardMarkup:
    def cb(act: str) -> str:
        return RecordCB(act=act, id=item.id, src=src, page=page, ftype=ftype, fstat=fstat).pack()

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, удалить", callback_data=cb("dy")),
                InlineKeyboardButton(text="◀️ Отмена", callback_data=cb("o")),
            ]
        ]
    )


# -------------------------------------------------------------------- настройки


SETTINGS_HOURS = [17, 18, 19, 20, 21, 22, 23]


def settings_text(user: User) -> str:
    return (
        "Настройки\n\n"
        f"Вечерний чек-ин: {user.checkin_hour:02d}:00 по вашему времени.\n"
        "Часовой пояс пока общий для всех — Москва."
    )


def settings_keyboard(user: User) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🕗 Время чек-ина: {user.checkin_hour:02d}:00",
                    callback_data=SettingsCB(act="hm").pack(),
                )
            ]
        ]
    )


def hours_keyboard(user: User) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=("• " if hour == user.checkin_hour else "") + f"{hour:02d}:00",
                callback_data=SettingsCB(act="h", val=hour).pack(),
            )
            for hour in SETTINGS_HOURS[start : start + 4]
        ]
        for start in range(0, len(SETTINGS_HOURS), 4)
    ]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=SettingsCB(act="b").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)
