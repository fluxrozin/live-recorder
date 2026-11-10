FROM python:3.12-slim-bookworm AS runtime

# Debian bookworm の FFmpeg 5.1 は TikTok FLV の HEVC（codec id 12）を読めず、MP4 化が 0 バイトになる。
# 8.1 以降がその形式を demux できる。LGPL の配布バイナリを固定して入れる。
ARG FFMPEG_URL=https://github.com/FFmpeg-Builds/FFmpeg-Builds/releases/download/autobuild-2026-11-08-12-35/ffmpeg-n8.1.3-34-ga35157abb9-linux64-lgpl-8.1.tar.xz
ARG FFMPEG_SHA256=d8f66e659eff73333b1125fd28a47c440ecac38349fe37b79aa201ed56a481c0

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl xz-utils \
    && curl -fsSL "$FFMPEG_URL" -o /tmp/ffmpeg.tar.xz \
    && echo "${FFMPEG_SHA256}  /tmp/ffmpeg.tar.xz" | sha256sum -c - \
    && tar -xJf /tmp/ffmpeg.tar.xz -C /tmp \
    && install -m 755 /tmp/ffmpeg-n8.1.3-34-ga35157abb9-linux64-lgpl-8.1/bin/ffmpeg /usr/local/bin/ffmpeg \
    && install -m 755 /tmp/ffmpeg-n8.1.3-34-ga35157abb9-linux64-lgpl-8.1/bin/ffprobe /usr/local/bin/ffprobe \
    && rm -rf /tmp/ffmpeg.tar.xz /tmp/ffmpeg-n8.1.3-34-ga35157abb9-linux64-lgpl-8.1 \
    && apt-get purge -y curl xz-utils \
    && apt-get autoremove -y \
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

FROM runtime AS transcriber

RUN pip install --no-cache-dir "/app[speech]"

ENV LD_LIBRARY_PATH=/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib \
    HF_HOME=/data/config/huggingface

FROM runtime
