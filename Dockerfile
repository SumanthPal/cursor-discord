FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    PROJECTS_PATH=/app/projects.yaml \
    BRIDGE_WORKSPACE=/tmp/cursor-discord-bridge

WORKDIR /app

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /tmp/cursor-discord-bridge \
    && chown -R appuser:appuser /app /tmp/cursor-discord-bridge

COPY pyproject.toml README.md ./
COPY bot ./bot
COPY projects.example.yaml ./projects.example.yaml

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir . \
    && cp projects.example.yaml /app/projects.yaml \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"

CMD ["python", "-m", "bot"]
