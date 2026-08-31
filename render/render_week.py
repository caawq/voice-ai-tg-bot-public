"""
Рендер картинки "хронология недели" из структурированных данных.

Использование:
    python3 render_week.py

Или как функция:
    from render_week import render_week_image
    render_week_image(data, theme="light", out_path="week.png")

Данные — это обычный dict/JSON, который в реальном боте будет собираться
из базы задач/событий/целей пользователя. Ничего, кроме этой структуры,
шаблон week_card.html.jinja не знает и не хочет знать — это и есть суть
"шаблона": один HTML-файл с версткой + любые данные на входе.
"""

import os
import pathlib

from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright

TEMPLATE_DIR = pathlib.Path(__file__).parent
# Путь к Chromium. Пусто (обычный случай) — Playwright берёт свой собственный
# браузер, установленный командой `playwright install chromium`. Переменная нужна
# только там, где Chromium лежит в нестандартном месте (например, в CI-образе).
CHROMIUM_PATH = os.environ.get("CHROMIUM_PATH", "")


def render_week_image(data: dict, theme: str, out_path: str) -> None:
    """
    data: {
        "date_range": "11–17 августа",
        "goal": {"label": "Английский язык", "percent": 40} | None,
        "days": [
            {
                "name": "Пн", "num": 11,
                "items": [
                    {"type": "event", "time": "10:00", "label": "Созвон"},
                    {"type": "task", "label": "Прочитать статью"},
                    {"type": "task_done", "label": "Купить корм"},
                    {"type": "overdue", "label": "Написать отчёт"},
                ],
            },
            ...  # ровно 7 дней, Пн..Вс
        ],
    }
    theme: "light" | "dark"
    out_path: куда сохранить PNG
    """
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
    template = env.get_template("week_card.html.jinja")
    html = template.render(theme=theme, **data)

    html_path = pathlib.Path(out_path).with_suffix(".render.html")
    html_path.write_text(html, encoding="utf-8")

    with sync_playwright() as p:
        launch_kwargs = {"executable_path": CHROMIUM_PATH} if CHROMIUM_PATH else {}
        browser = p.chromium.launch(**launch_kwargs)
        page = browser.new_page(viewport={"width": 1200, "height": 200}, device_scale_factor=1)
        page.goto("file://" + str(html_path.resolve()))
        # .week-card сам определяет свою высоту по контенту (сколько задач в неделе) —
        # скриншотим именно элемент, а не фиксированную область, чтобы шаблон одинаково
        # хорошо работал и для лёгкой, и для загруженной недели.
        page.locator(".week-card").screenshot(path=out_path)
        browser.close()

    html_path.unlink()  # временный файл больше не нужен


SAMPLE_DATA = {
    "date_range": "11–17 августа",
    "goal": {"label": "Английский язык", "percent": 40},
    "days": [
        {"name": "Пн", "num": 11, "items": [
            {"type": "event", "time": "10:00", "label": "Созвон"},
            {"type": "task_done", "label": "Купить корм"},
            {"type": "task", "label": "Прочитать статью"},
        ]},
        {"name": "Вт", "num": 12, "items": [
            {"type": "overdue", "label": "Написать отчёт"},
            {"type": "event", "time": "18:30", "label": "Тренировка"},
        ]},
        {"name": "Ср", "num": 13, "items": []},
        {"name": "Чт", "num": 14, "items": [
            {"type": "event", "time": "09:00", "label": "Клиент"},
            {"type": "task_done", "label": "Оплатить интернет"},
        ]},
        {"name": "Пт", "num": 15, "items": [
            {"type": "task", "label": "Собрать вещи"},
            {"type": "task", "label": "Купить билет"},
        ]},
        {"name": "Сб", "num": 16, "items": [
            {"type": "event", "time": "12:00", "label": "ДР Ани"},
        ]},
        {"name": "Вс", "num": 17, "items": []},
    ],
}

if __name__ == "__main__":
    render_week_image(SAMPLE_DATA, theme="light", out_path=str(TEMPLATE_DIR / "sample-light.png"))
    render_week_image(SAMPLE_DATA, theme="dark", out_path=str(TEMPLATE_DIR / "sample-dark.png"))
    print("готово: sample-light.png, sample-dark.png")
