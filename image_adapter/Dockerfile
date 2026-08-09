ARG PYTHON_IMAGE=mirror.ccs.tencentyun.com/library/python:3.12-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /adapter
RUN addgroup --gid 1000 adapter \
    && adduser --disabled-password --gecos "" --uid 1000 --gid 1000 --home /adapter adapter

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY __init__.py server.py ./image_adapter/

USER adapter
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/health', timeout=3).read()"]

CMD ["python", "-m", "image_adapter.server"]
