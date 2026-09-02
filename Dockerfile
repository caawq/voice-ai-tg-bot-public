# Образ бота. python:3.12-slim — компромисс между размером и тем, что в нём уже
# есть Python нужной версии; requirements.txt сам просит tzdata (см. комментарий
# там), поэтому в apt отдельно её ставить не нужно.
FROM python:3.12-slim

# Логи идут в docker compose logs сразу, а не пачками при выходе процесса;
# .pyc-файлы в контейнере не нужны — образ и так одноразовый.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Зависимости отдельным слоем: Docker пересоберёт этот слой, только если
# изменился requirements.txt, а не на каждую правку кода бота.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "-m", "bot.main"]
