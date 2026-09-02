"""Инлайн-клавиатуры бота."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# Короткие префиксы ("v:y:", "v:c:"), а не длинные "voice:confirm:" — экономят
# место в лимите callback_data на 64 байта вместе с attempt_id.
CONFIRM_PREFIX = "v:y:"
CORRECT_PREFIX = "v:c:"


def confirmation_keyboard(attempt_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, сохранить", callback_data=f"{CONFIRM_PREFIX}{attempt_id}"),
                InlineKeyboardButton(text="Исправить", callback_data=f"{CORRECT_PREFIX}{attempt_id}"),
            ]
        ]
    )


# Вечерний чек-ин (Промпт 4): три действия на одну задачу, item.id прямо в
# callback_data — доп. состояние в bot/state.py не нужно, задача сама себе
# идентификатор.
CHECKIN_POSTPONE_PREFIX = "c:p:"
CHECKIN_KEEP_PREFIX = "c:k:"
CHECKIN_DELETE_PREFIX = "c:d:"


def checkin_task_keyboard(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Перенести на завтра", callback_data=f"{CHECKIN_POSTPONE_PREFIX}{item_id}"
                ),
                InlineKeyboardButton(text="Оставить", callback_data=f"{CHECKIN_KEEP_PREFIX}{item_id}"),
                InlineKeyboardButton(text="Удалить", callback_data=f"{CHECKIN_DELETE_PREFIX}{item_id}"),
            ]
        ]
    )
