"""
Вход в бота и справка: /start, /help и меню команд (Промпт 6, пп. 1-3).

Часовой пояс на этом шаге не спрашиваем — все пользователи считаются
московскими (services/users.DEFAULT_TIMEZONE). Это осознанное упрощение
шага, а не забытый онбординг: выбор пояса появится в /settings отдельным
шагом, тогда же и в приветствии.
"""

from __future__ import annotations

from aiogram import Bot, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, BotCommandScopeDefault, Message

from db.session import session_scope
from services.users import get_or_create_user

router = Router(name="navigation")

BOT_COMMANDS = [
    BotCommand(command="today", description="Что сегодня"),
    BotCommand(command="list", description="Все записи"),
    BotCommand(command="settings", description="Настройки"),
    BotCommand(command="help", description="Как пользоваться"),
    BotCommand(command="start", description="Начать заново"),
]

START_TEXT = (
    "Привет! Я превращаю голосовые в понятный план.\n\n"
    "Просто надиктуйте, что нужно — одним сообщением, как рассказали бы живому "
    "человеку: «завтра в 15 созвон с клиентом, купить корм коту и не забыть про "
    "английский». Я разберу это на события, задачи и цели, покажу, как понял, "
    "и сохраню только после вашего подтверждения.\n\n"
    "Что дальше:\n"
    "/today — что на сегодня\n"
    "/list — все записи с фильтрами\n"
    "/week — картинка недели\n"
    "/settings — настройки\n"
    "/help — подробнее\n\n"
    "Начните с голосового — остальное покажу по ходу."
)

HELP_TEXT = (
    "Как пользоваться\n\n"
    "Голосом. Надиктуйте всё подряд, одним сообщением. Я разберу его на записи "
    "трёх видов: событие (есть точное время), задача (есть день или совсем без "
    "даты) и цель (проценты прогресса, без срока). На каждую запись покажу "
    "карточку с кнопками «Да, сохранить» и «Исправить» — пока не подтвердите, "
    "в базу ничего не уйдёт.\n\n"
    "Если понял неправильно — нажмите «Исправить» и скажите, что не так "
    "(«не в 15, а в 17»). Я переспрошу с учётом правки, а не начну с нуля.\n\n"
    "Команды\n"
    "/today — активные и просроченные записи на сегодня\n"
    "/list — все записи; фильтры по типу и статусу переключаются нажатием\n"
    "/week — картинка недели; под ней кнопки на каждую запись\n"
    "/settings — время вечернего чек-ина\n\n"
    "Вечером я сам спрошу, что делать с невыполненным, а утром понедельника "
    "пришлю картинку недели."
)


async def setup_bot_commands(bot: Bot) -> None:
    """Меню команд в интерфейсе Telegram (кнопка «Меню» рядом с полем ввода)."""
    await bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeDefault())


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    # Заводим пользователя сразу на /start, а не при первом голосовом: так к
    # моменту первой записи у него уже есть пояс и настройки по умолчанию.
    async with session_scope() as session:
        await get_or_create_user(session, message.from_user.id)

    await message.answer(START_TEXT)


@router.message(Command("help"))
async def handle_help(message: Message) -> None:
    await message.answer(HELP_TEXT)
