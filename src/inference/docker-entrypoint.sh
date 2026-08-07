#!/usr/bin/env bash
set -euo pipefail

: "${HF_TOKEN:?HF_TOKEN is required for the gated Apertus model}"
: "${VLLM_API_KEY:?VLLM_API_KEY is required}"

model_id="${MODEL_ID:-swiss-ai/Apertus-v1.5-8B}"
max_model_len="${MAX_MODEL_LEN:-32768}"
gpu_memory_utilization="${GPU_MEMORY_UTILIZATION:-0.85}"
max_num_seqs="${MAX_NUM_SEQS:-8}"
limit_mm_per_prompt="${LIMIT_MM_PER_PROMPT:-}"
if [[ -z "${limit_mm_per_prompt}" ]]; then
  limit_mm_per_prompt='{"image":4,"audio":1}'
fi

args=(
  serve "${model_id}"
  --host 0.0.0.0
  --port 8000
  --chat-template-content-format string
  --gpu-memory-utilization "${gpu_memory_utilization}"
  --max-model-len "${max_model_len}"
  --max-num-seqs "${max_num_seqs}"
  --limit-mm-per-prompt "${limit_mm_per_prompt}"
  --safetensors-load-strategy eager
  --enable-prefix-caching
  --enable-auto-tool-choice
  --tool-call-parser apertus
  --reasoning-parser apertus
)

if [[ -n "${MODEL_REVISION:-}" ]]; then
  args+=(--revision "${MODEL_REVISION}")
fi

if [[ "${VLLM_ENFORCE_EAGER:-false}" == "true" ]]; then
  args+=(--enforce-eager)
fi

if [[ "${DRY_RUN:-false}" == "true" ]]; then
  printf 'vllm'
  printf ' %q' "${args[@]}"
  printf '\n'
  exit 0
fi

exec vllm "${args[@]}"