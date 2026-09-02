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
