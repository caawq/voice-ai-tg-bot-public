"""
Список записей и карточка записи (Промпт 6, пп. 4 и 5).

Один и тот же экран обслуживает три входа: /list (с фильтрами), /today и
клавиатуру под обложкой недели. Отличает их только код src в callback_data —
он же решает, куда вернёт кнопка «Назад» и какой запрос перечитать после
изменения записи. Поэтому карточка здесь ровно одна на всех, а не три почти
одинаковых.

Любое действие — своя транзакция (session_scope), как и в вечернем чек-ине:
одно нажатие — одно изменение, откат при ошибке.
"""

from __future__ import annotations

import datetime as dt

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot import views
from bot.callbacks import SRC_LIST, SRC_TODAY, SRC_WEEK, ListCB, RecordCB
from db.models import Item, User
from db.session import session_scope
from services import items as items_svc
from services import records as records_svc
from services import timeframe
from services.users import get_or_create_user

router = Router(name="records")

GONE = "Записи больше нет — возможно, она уже удалена."


async def _load_page(
    session, user: User, src: str, page: int, ftype: str, fstat: str, today: dt.date
) -> tuple[list[Item], int, int]:
    """Страница нужного экрана: (записи, всего страниц, номер страницы после нормализации)."""
    if src == SRC_LIST:
        items, total_pages = await records_svc.list_page(
            session, user, ftype=ftype, fstat=fstat, today=today, page=page
        )
        return items, total_pages, max(0, min(page, total_pages - 1))

    if src == SRC_TODAY:
        everything = await records_svc.today_records(session, user, today)
    else:  # SRC_WEEK
        monday = timeframe.week_start(today)
        everything = await items_svc.week_items(session, user, monday)
        everything.sort(key=lambda i: views.week_sort_key(i, user.timezone))

    size = records_svc.PAGE_SIZE
    total_pages = max(1, (len(everything) + size - 1) // size)
    page = max(0, min(page, total_pages - 1))
    return everything[page * size : (page + 1) * size], total_pages, page


def _list_markup(src, items, page, total_pages, ftype, fstat, today, timezone):
    if src == SRC_WEEK:
        return views.week_keyboard(items, page, total_pages, today, timezone)
    return views.list_keyboard(src, items, page, total_pages, ftype, fstat, today, timezone)


async def _show_list(
    message: Message, *, telegram_id: int, src: str, page: int, ftype: str, fstat: str, edit: bool
) -> None:
    """
    telegram_id приходит параметром, а не берётся из message: у сообщения,
    которое прислал бот (а именно его редактирует callback), from_user — это
    сам бот, а chat.id совпадает с id пользователя только в личке.
    """
    async with session_scope() as session:
        user = await get_or_create_user(session, telegram_id)
        today = timeframe.today(user.timezone)
        items, total_pages, page = await _load_page(session, user, src, page, ftype, fstat, today)
        timezone = user.timezone

    text = views.list_text(src, items, page, total_pages, ftype, fstat)
    markup = _list_markup(src, items, page, total_pages, ftype, fstat, today, timezone)

    if edit:
        await views.edit_view(message, text, markup)
    else:
        await message.answer(text, reply_markup=markup)


@router.message(Command("list"))
async def handle_list(message: Message) -> None:
    await _show_list(
        message, telegram_id=message.from_user.id, src=SRC_LIST, page=0, ftype="a", fstat="a", edit=False
    )


@router.message(Command("today"))
async def handle_today(message: Message) -> None:
    await _show_list(
        message, telegram_id=message.from_user.id, src=SRC_TODAY, page=0, ftype="a", fstat="a", edit=False
    )


@router.callback_query(ListCB.filter())
async def handle_list_callback(callback: CallbackQuery, callback_data: ListCB) -> None:
    if callback_data.act == "n":  # кнопка-счётчик страниц: показываем, но никуда не ведёт
        await callback.answer()
        return

    ftype, fstat, page = callback_data.ftype, callback_data.fstat, callback_data.page
    if callback_data.act == "ft":
        ftype, page = records_svc.next_type(ftype), 0
    elif callback_data.act == "fs":
        fstat, page = records_svc.next_status(fstat), 0

    await _show_list(
        callback.message,
        telegram_id=callback.from_user.id,
        src=callback_data.src,
        page=page,
        ftype=ftype,
        fstat=fstat,
        edit=True,
    )
    await callback.answer()


async def _show_card(callback: CallbackQuery, data: RecordCB, *, note: str | None = None) -> None:
    async with session_scope() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        today = timeframe.today(user.timezone)
        item = await records_svc.get_owned(session, data.id, callback.from_user.id)
        timezone = user.timezone

    if item is None:
        await callback.answer(GONE, show_alert=True)
        await _show_list(
            callback.message,
            telegram_id=callback.from_user.id,
            src=data.src,
            page=data.page,
            ftype=data.ftype,
            fstat=data.fstat,
            edit=True,
        )
        return

    await views.edit_view(
        callback.message,
        views.card_text(item, today, timezone),
        views.card_keyboard(item, data.src, data.page, data.ftype, data.fstat),
    )
    await callback.answer(note or "")


@router.callback_query(RecordCB.filter(F.act == "o"))
async def handle_open(callback: CallbackQuery, callback_data: RecordCB) -> None:
    await _show_card(callback, callback_data)


@router.callback_query(RecordCB.filter(F.act == "b"))
async def handle_back(callback: CallbackQuery, callback_data: RecordCB) -> None:
    await _show_list(
        callback.message,
        telegram_id=callback.from_user.id,
        src=callback_data.src,
        page=callback_data.page,
        ftype=callback_data.ftype,
        fstat=callback_data.fstat,
        edit=True,
    )
    await callback.answer()


@router.callback_query(RecordCB.filter(F.act == "d"))
async def handle_done(callback: CallbackQuery, callback_data: RecordCB) -> None:
    async with session_scope() as session:
        item = await records_svc.get_owned(session, callback_data.id, callback.from_user.id)
        if item is None:
            await callback.answer(GONE, show_alert=True)
            return
        records_svc.mark_done(item)

    await _show_card(callback, callback_data, note="Отмечено как выполненное")


@router.callback_query(RecordCB.filter(F.act == "pm"))
async def handle_postpone_menu(callback: CallbackQuery, callback_data: RecordCB) -> None:
    async with session_scope() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        today = timeframe.today(user.timezone)
        item = await records_svc.get_owned(session, callback_data.id, callback.from_user.id)
        timezone = user.timezone

    if item is None:
        await callback.answer(GONE, show_alert=True)
        return

    await views.edit_view(
        callback.message,
        views.card_text(item, today, timezone) + "\n\nНа когда перенести?",
        views.postpone_keyboard(item, callback_data.src, callback_data.page, callback_data.ftype, callback_data.fstat),
    )
    await callback.answer()


@router.callback_query(RecordCB.filter(F.act == "p"))
async def handle_postpone(callback: CallbackQuery, callback_data: RecordCB) -> None:
    async with session_scope() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        today = timeframe.today(user.timezone)
        item = await records_svc.get_owned(session, callback_data.id, callback.from_user.id)
        if item is None:
            await callback.answer(GONE, show_alert=True)
            return

        target = {
            "t": today,
            "m": today + dt.timedelta(days=1),
            "w": today + dt.timedelta(days=7),
        }[callback_data.arg]
        records_svc.postpone(item, target, user.timezone)

    labels = {"t": "на сегодня", "m": "на завтра", "w": "на неделю вперёд"}
    await _show_card(callback, callback_data, note=f"Перенесено {labels[callback_data.arg]}")


@router.callback_query(RecordCB.filter(F.act == "da"))
async def handle_delete_ask(callback: CallbackQuery, callback_data: RecordCB) -> None:
    async with session_scope() as session:
        item = await records_svc.get_owned(session, callback_data.id, callback.from_user.id)

    if item is None:
        await callback.answer(GONE, show_alert=True)
        return

    await views.edit_view(
        callback.message,
        f"Удалить запись «{item.title}»?",
        views.delete_confirm_keyboard(item, callback_data.src, callback_data.page, callback_data.ftype, callback_data.fstat),
    )
    await callback.answer()


@router.callback_query(RecordCB.filter(F.act == "dy"))
async def handle_delete(callback: CallbackQuery, callback_data: RecordCB) -> None:
    async with session_scope() as session:
        item = await records_svc.get_owned(session, callback_data.id, callback.from_user.id)
        if item is None:
            await callback.answer(GONE, show_alert=True)
            return
        records_svc.soft_delete(item)

    await _show_list(
        callback.message,
        telegram_id=callback.from_user.id,
        src=callback_data.src,
        page=callback_data.page,
        ftype=callback_data.ftype,
        fstat=callback_data.fstat,
        edit=True,
    )
    await callback.answer("Удалено")
