# Apertus inference container

This image derives from the Apertus team's official amd64 release image pinned
to OCI digest `sha256:d85533f251c8272218ae2e519510836376e5a335b44f2204f193018e37753bc6`.
It starts the Swiss AI vLLM fork with the Apertus tool and reasoning parsers.

## Local run

The runtime requires an NVIDIA GPU with a driver compatible with CUDA 13 and a
Hugging Face token whose account accepted the Apertus model terms.

```bash
docker build -t apertus-vllm:local .
docker run --rm --gpus all -p 8000:8000 \
  -e HF_TOKEN=hf_your_read_token \
  -e VLLM_API_KEY=local-development-key \
  -v "$HOME/.cache/huggingface:/home/appuser/hf-home" \
  apertus-vllm:local
```

The OpenAI-compatible base URL is `http://localhost:8000/v1`. In Azure, ingress
is internal and the API key is available only to the Chainlit frontend. vLLM
reads `VLLM_API_KEY` directly from the environment; the entrypoint never copies
the key into process arguments or dry-run output.

## Runtime settings

| Variable | Default |
| --- | --- |
| `MODEL_ID` | `swiss-ai/Apertus-v1.5-8B` |
| `MODEL_REVISION` | Model repository default; set an immutable commit for deployment |
| `MAX_MODEL_LEN` | `32768` |
| `GPU_MEMORY_UTILIZATION` | `0.85` |
| `MAX_NUM_SEQS` | `8` |
| `LIMIT_MM_PER_PROMPT` | `{"image":4,"audio":1}` |
| `VLLM_ENFORCE_EAGER` | `false` |

The model supports up to 262,144 tokens, but larger contexts consume more KV
cache and reduce concurrency on a single A100.

Use `DRY_RUN=true` to print the escaped vLLM command without starting the
server. The Azure deployment mounts `/home/appuser/hf-home` from Azure Files and
exposes port 8000 only through internal Container Apps ingress.