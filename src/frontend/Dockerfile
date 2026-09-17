FROM ghcr.io/astral-sh/uv:0.12.15@sha256:62f8c047d0a0e9ece6b53fc63df902585a67a47a7f318ddec4a37db586edc8e3 AS uv

FROM python:3.13.15-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e

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
RUN uv export --frozen --no-dev --no-emit-project --format requirements-txt --output-file /tmp/requirements.txt \
  && uv venv /app/.venv \
  && uv pip sync --python /app/.venv/bin/python --index-url "${PYTHON_PACKAGE_INDEX_URL}" \
    --require-hashes /tmp/requirements.txt \
  && rm /tmp/requirements.txt

COPY --chown=appuser:appuser .chainlit/config.toml ./.chainlit/config.toml
COPY --chown=appuser:appuser public ./public
COPY --chown=appuser:appuser apertus_frontend ./apertus_frontend
COPY --chown=appuser:appuser app.py chainlit.md safety_evaluation.py ./

RUN mkdir -p /app/.files /app/.chainlit/translations \
  && chown appuser:appuser /app \
  && chown -R appuser:appuser /app/.files /app/.chainlit

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]

CMD ["chainlit", "run", "app.py", "--host", "0.0.0.0", "--port", "8000", "-h"]