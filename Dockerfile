FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DOWNLOAD_DIR=/downloads \
    SESSION_DIR=/sessions \
    CONFIG_DIR=/config \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8000

WORKDIR /app

RUN addgroup --system bot && adduser --system --ingroup bot bot

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /downloads /sessions /config && chown -R bot:bot /app /downloads /sessions /config

USER bot

EXPOSE 8000

VOLUME ["/downloads", "/sessions", "/config"]

CMD ["sh", "-c", "uvicorn app.main:app --host ${WEB_HOST} --port ${WEB_PORT}"]
