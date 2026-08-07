FROM ghcr.io/astral-sh/uv:0.11.30@sha256:173b74e36e4bddd0546771a7041cae9f65b3ad28964eb22a2853bca944b2c67a AS uv

FROM python:3.12.10-slim-bookworm@sha256:97983fa8cc88343512862c62307159a82261c3528dc025f79e5a3f7af43e50b4

LABEL org.opencontainers.image.source="https://github.com/francesco-sodano/aca-apertus1.5-8B"
LABEL org.opencontainers.image.description="Grounded Chainlit frontend for Apertus 1.5"
LABEL org.opencontainers.image.licenses="MIT"

ARG PYTHON_PACKAGE_INDEX_URL=https://pypi.org/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

COPY --from=uv /uv /uvx /bin/

RUN groupadd --system --gid 10001 appuser \
    && useradd --system --uid 10001 --gid appuser --create-home appuser

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
RUN UV_DEFAULT_INDEX="${PYTHON_PACKAGE_INDEX_URL}" uv lock --refresh \
  && UV_DEFAULT_INDEX="${PYTHON_PACKAGE_INDEX_URL}" uv sync --frozen --no-dev --no-install-project

COPY --chown=appuser:appuser .chainlit/config.toml ./.chainlit/config.toml
COPY --chown=appuser:appuser public ./public
COPY --chown=appuser:appuser apertus_frontend ./apertus_frontend
COPY --chown=appuser:appuser app.py chainlit.md ./

RUN mkdir -p /app/.files /app/.chainlit/translations \
  && chown -R appuser:appuser /app/.files /app/.chainlit

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]

CMD ["chainlit", "run", "app.py", "--host", "0.0.0.0", "--port", "8000", "-h"]