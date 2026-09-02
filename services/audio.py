"""
Подготовка аудио к транскрипции.

Telegram отдаёт голосовые в OGG/Opus. Gemini по документации умеет понимать
этот формат напрямую через свой родной API, но в примерах OpenAI-совместимого
слоя (services/llm.py) фигурирует только "format": "wav" — не факт, что
совместимый эндпоинт валидирует произвольные значения так же щедро, как
родной. Конвертация в WAV перед отправкой убирает этот вопрос совсем: WAV
подходит любому провайдеру, который вообще умеет обрабатывать `input_audio`,
а не только Gemini.

Конвертирует ffmpeg — бинарник, а не Python-пакет, поэтому в requirements.txt
его нет; он должен быть в образе (см. Dockerfile) или в PATH при локальном
запуске.
"""

from __future__ import annotations

import asyncio


class AudioConversionError(RuntimeError):
    """ffmpeg не смог обработать файл: битые данные, бинарника нет в PATH и т.п."""


async def ogg_to_wav(raw_ogg: bytes) -> bytes:
    """
    Перекодировать OGG/Opus (голосовое Telegram) в WAV (16 кГц, моно).

    16 кГц и один канал — стандартный вход для распознавания речи, не нужно
    гонять через сеть исходное качество: голос человека прекрасно умещается
    в этот битрейт, а файл выходит в разы меньше.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0",       # вход — из stdin, не из временного файла
            "-ar", "16000",       # 16 кГц
            "-ac", "1",           # моно
            "-f", "wav",
            "pipe:1",             # выход — в stdout
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise AudioConversionError(
            "ffmpeg не найден в PATH. В Docker-образе он ставится через apt "
            "(см. Dockerfile); при локальном запуске без Docker установите его "
            "отдельно."
        ) from exc

    stdout, stderr = await process.communicate(input=raw_ogg)
    if process.returncode != 0:
        raise AudioConversionError(f"ffmpeg завершился с ошибкой: {stderr.decode(errors='replace')[:300]}")
    if not stdout:
        raise AudioConversionError("ffmpeg вернул пустой файл")
    return stdout
