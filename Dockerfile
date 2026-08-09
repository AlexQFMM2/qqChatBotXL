ARG PYTHON_IMAGE=mirror.ccs.tencentyun.com/library/python:3.12-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN addgroup --gid 1000 bot \
    && adduser --disabled-password --gecos "" --uid 1000 --gid 1000 --home /app bot

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY persona.md ./persona.md
COPY bq ./emotes
COPY assets/fonts/DroidSansFallbackFull.ttf ./fonts/DroidSansFallbackFull.ttf

RUN mkdir -p /app/data && chown -R bot:bot /app
USER bot

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).read()"]

CMD ["python", "-m", "app.main"]
