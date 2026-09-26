FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY vitool ./vitool
RUN pip install --no-cache-dir . && useradd --create-home --uid 10001 watcher && mkdir /data && chown watcher /data
USER watcher
ENV VITOOL_DATA_DIR=/data VITOOL_CONTAINER=1 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/state', timeout=3).read()"]
STOPSIGNAL SIGINT
CMD ["vitool"]
