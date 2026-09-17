# Apertus inference container

This image derives from the Apertus team's official amd64 release image pinned
to OCI digest `sha256:d85533f251c8272218ae2e519510836376e5a335b44f2204f193018e37753bc6`.
It starts the Swiss AI vLLM fork with the Apertus tool and reasoning parsers.
Follow the version-specific
[model card](https://huggingface.co/swiss-ai/Apertus-v1.5-8B) and
[vLLM serving guide](https://apertus-ai.org/docs/guides/vllm/) when updating it.
Do not replace the bundle with upstream vLLM solely because the upstream parser
exists; the 1.5 multimodal model/Transformers integration has separate requirements.

## Local run

The runtime requires an NVIDIA GPU with a driver compatible with CUDA 13 and a
Hugging Face token whose account accepted the Apertus model terms.

```bash
docker build -t apertus-vllm:local .
docker run --rm --gpus all -p 127.0.0.1:8000:8000 \
  -e HF_TOKEN -e VLLM_API_KEY -e MODEL_REVISION \
  -v "$HOME/.cache/huggingface:/home/appuser/hf-home" \
  apertus-vllm:local
```

Set `HF_TOKEN`, `VLLM_API_KEY`, and an immutable `MODEL_REVISION` in your shell
before running this example. Do not put credentials into the Dockerfile or
command arguments. Both credentials must be nonempty even for `DRY_RUN=true`.

The OpenAI-compatible base URL is `http://localhost:8000/v1`. In Azure, ingress
is internal and the API key is available only to the Chainlit frontend.

## Runtime settings

| Variable | Default |
| --- | --- |
| `HF_TOKEN` | Required; gated-model download credential |
| `VLLM_API_KEY` | Required; inherited by vLLM, never expanded into its command line |
| `MODEL_ID` | `swiss-ai/Apertus-v1.5-8B` |
| `MODEL_REVISION` | Model repository default; set an immutable commit for deployment |
| `MAX_MODEL_LEN` | `32768` |
| `GPU_MEMORY_UTILIZATION` | `0.85` |
| `MAX_NUM_SEQS` | `8` |
| `LIMIT_MM_PER_PROMPT` | `{"image":4,"audio":1}` |
| `VLLM_ENFORCE_EAGER` | `false` |
| `HF_HOME` | `/home/appuser/hf-home` in the image |
| `VLLM_CACHE_ROOT` | `/home/appuser/hf-home/.vllm-cache` in the image |
| `DRY_RUN` | `false`; prints the escaped non-secret command when `true` |

The model supports up to 262,144 tokens, but larger contexts consume more KV
cache and reduce concurrency on a single A100.

Use `DRY_RUN=true` to print the escaped vLLM command without starting the
server. The Azure deployment mounts `/home/appuser/hf-home` from Azure Files and
exposes port 8000 only through internal Container Apps ingress.

## Serving and Tracing

The image runs as user 10001 and starts vLLM with prefix caching, eager
safetensors loading, automatic tool choice, the `apertus` tool parser, and the
`apertus` reasoning parser. Frontend request templates choose whether thinking is
enabled; the inference image itself is shared by both chat profiles.

The frontend traces selector calls and full streamed generation, requesting
`stream_options.include_usage=true` for the latter. Reported prompt/completion
tokens, response IDs, finish reasons, and latency appear in the existing
Application Insights resource. No trace exporter or GPU profiler is added to this
image. Client spans do not expose internal kernels, KV-cache state, or hidden
reasoning. Configure `TRACE_CONTENT` on the **frontend**, not on this container;
see [inference tracing](../../docs/operations.md#inference-tracing).

On Azure the app uses 24 CPU cores and 220 GiB container memory on the serverless
A100 profile, scales from zero to one replica, and cools down after 30 minutes.
Startup probes allow approximately 15 minutes. `/health` is a server health
check, not proof that a particular completion/media request succeeds.

Inference is intentionally absent from azd's service list. The postprovision
hook builds an environment/source-tree-tagged image and requires successful ACR
artifact-stream conversion before promotion. See [image promotion and rollback](../../docs/operations.md#image-promotion).