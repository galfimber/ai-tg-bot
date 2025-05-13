FROM python:3.11-slim-bookworm

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc python3-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd -m botuser && \
    chown -R botuser:botuser /app
USER botuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    TZ=Europe/Moscow

CMD ["python", "bot.py"]