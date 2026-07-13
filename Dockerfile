FROM python:3.12-slim

ARG APP_VERSION=dev
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_VERSION=${APP_VERSION} \
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

RUN mkdir -p /downloads/images /downloads/videos /downloads/files /sessions /config \
    && chown -R bot:bot /app /downloads /sessions /config

USER bot

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)" || exit 1

VOLUME ["/downloads", "/sessions", "/config"]

CMD ["python", "-m", "app.serve"]
