# Образ бота. python:3.12-slim — компромисс между размером и тем, что в нём уже
# есть Python нужной версии; requirements.txt сам просит tzdata (см. комментарий
# там), поэтому в apt отдельно её ставить не нужно.
FROM python:3.12-slim

# Логи идут в docker compose logs сразу, а не пачками при выходе процесса;
# .pyc-файлы в контейнере не нужны — образ и так одноразовый.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# ffmpeg — конвертация голосовых Telegram (OGG/Opus) в WAV перед отправкой
# на транскрипцию, см. services/audio.py. Это системный бинарник, не
# ставится через pip. --no-install-recommends и чистка apt-кэша сразу же —
# чтобы не тащить в образ рекомендованный мусор и списки пакетов.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Зависимости отдельным слоем: Docker пересоберёт этот слой, только если
# изменился requirements.txt, а не на каждую правку кода бота.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright сам скачивает Chromium, но не системные библиотеки под него —
# на голом python:3.12-slim рендер (render/render_week.py, Промпт 5) без
# этого падает на старте ("Executable doesn't exist" / отсутствующие .so).
# --with-deps ставит и то, и другое через apt за один шаг.
RUN playwright install --with-deps chromium

COPY . .

CMD ["python", "-m", "bot.main"]
