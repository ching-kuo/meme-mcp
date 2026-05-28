FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY . .
RUN uv sync --locked --no-dev
RUN useradd --create-home --uid 10001 appuser
USER appuser
HEALTHCHECK CMD .venv/bin/python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD [".venv/bin/uvicorn", "meme_mcp.app:create_configured_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
