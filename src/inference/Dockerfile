FROM ghcr.io/swiss-ai/vllm_apertus_1.5_release:latest-amd64@sha256:d85533f251c8272218ae2e519510836376e5a335b44f2204f193018e37753bc6

LABEL org.opencontainers.image.source="https://github.com/francesco-sodano/aca-apertus1.5-8B"
LABEL org.opencontainers.image.description="Apertus 1.5 8B on the Swiss AI vLLM fork"
LABEL org.opencontainers.image.licenses="Apache-2.0 AND MIT"

USER root

RUN groupadd --system --gid 10001 appuser \
    && useradd --system --uid 10001 --gid appuser --create-home appuser \
    && mkdir -p /home/appuser/hf-home/.vllm-cache \
    && chown -R appuser:appuser /home/appuser

COPY LICENSE /licenses/aca-apertus-1.5-8b-MIT.txt
COPY docker-entrypoint.sh /usr/local/bin/apertus-entrypoint.sh
RUN chmod 0755 /usr/local/bin/apertus-entrypoint.sh

ENV HOME=/home/appuser \
    HF_HOME=/home/appuser/hf-home \
    HF_HUB_ENABLE_HF_TRANSFER=1 \
    VLLM_CACHE_ROOT=/home/appuser/hf-home/.vllm-cache \
    MODEL_ID=swiss-ai/Apertus-v1.5-8B \
    MAX_MODEL_LEN=32768 \
    GPU_MEMORY_UTILIZATION=0.85 \
    MAX_NUM_SEQS=8 \
    LIMIT_MM_PER_PROMPT='{"image":4,"audio":1}'

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15m --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5)"]

ENTRYPOINT ["/usr/local/bin/apertus-entrypoint.sh"]