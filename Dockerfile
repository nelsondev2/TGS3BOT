FROM python:3.11-slim

LABEL maintainer="telegram-s3-bot"
LABEL description="Bot de Telegram (Telethon) que sube archivos a S3 y los reenvía"

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY bot.py README.md .env.example ./

RUN useradd --create-home --uid 1000 app && \
    mkdir -p /tmp/tg_uploads && \
    chown -R app:app /app /tmp/tg_uploads

USER app

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:${PORT:-10000}/health || exit 1

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=10000 \
    DOWNLOAD_PATH=/tmp/tg_uploads \
    MAX_FILE_SIZE_MB=0 \
    TG_UPLOAD_LIMIT_MB=0 \
    LOG_LEVEL=INFO

EXPOSE 10000

CMD ["python", "bot.py"]
