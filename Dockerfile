FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md alembic.ini ./
COPY alembic ./alembic
COPY src ./src
COPY docker/entrypoint.sh /entrypoint.sh

RUN pip install --no-cache-dir . \
    && chmod a+x /entrypoint.sh \
    && chmod -R a+rX /app /entrypoint.sh

ENV PYTHONUNBUFFERED=1 \
    PYTHONPYCACHEPREFIX=/tmp/pycache \
    APP_HOME=/app \
    RECORDINGS_DIR=/data/recordings \
    CONFIG_DIR=/data/config

EXPOSE 8080
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "live_recorder.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
