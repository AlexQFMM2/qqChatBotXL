ARG PYTHON_IMAGE=mirror.ccs.tencentyun.com/library/python:3.12-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /adapter

RUN sed -i 's@deb.debian.org@mirrors.cloud.tencent.com@g; s@security.debian.org@mirrors.cloud.tencent.com@g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends build-essential ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN addgroup --gid 1000 adapter \
    && adduser --disabled-password --gecos "" --uid 1000 --gid 1000 --home /adapter adapter

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir \
        --index-url https://mirrors.cloud.tencent.com/pypi/simple \
        -r requirements.txt \
    && apt-get purge -y --auto-remove build-essential

COPY __init__.py server.py ./voice_adapter/
COPY reference.wav ./reference.wav

RUN mkdir -p /data && chown -R adapter:adapter /data
USER adapter

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8082/health', timeout=3).read()"]

CMD ["python", "-m", "voice_adapter.server"]
