# Fully local Apertus 1.5 8B

This optional Docker Desktop/WSL2 stack runs the original full Apertus model
and a Chainlit frontend on the laptop. It does not import the cloud application,
install Azure client packages, require Azure credentials, or enable cloud
authentication, moderation, grounding, or telemetry. The ACA deployment and
its default entrypoint remain unchanged.

Browser assets are bundled locally, use system fonts, and are protected by a
same-origin content policy. CDN-based mathematical typesetting is disabled;
math/calculator answers remain available as text. No external font or stylesheet
download is needed to open the UI.

## Separation from the Azure deployment

This is an opt-in bonus, not a replacement for the ACA sample:

| Surface | Azure default | Local bonus |
| --- | --- | --- |
| Entrypoint | `src/frontend/app.py` | `src/frontend/app_local.py` |
| Application namespace | `apertus_frontend` | `apertus_local`, plus shared pure tools/types |
| Dependencies | `src/frontend/pyproject.toml` and `uv.lock` | `src/frontend/local/pyproject.toml` and `uv.lock` |
| Image build | `src/frontend/Dockerfile` | `src/frontend/Dockerfile.local` |
| Orchestration | `azure.yaml`, Bicep, and deployment hooks | `local/compose.yaml`, project `apertus-local` |
| Configuration and credentials | Existing required Azure services and keys | Only `LOCAL_MODEL_ENDPOINT` and output limit; no runtime credentials |

`azd` still selects the cloud Dockerfile and entrypoint. The cloud image and
Python distribution do not include the local application namespace or
entrypoint. CI runs both dependency environments separately and checks the
cloud wheel/source archive for accidental bonus files. Starting or stopping
Compose does not provision, deploy, or modify any Azure resource.

## First-time setup

Use Docker Desktop with WSL2/Linux containers, Compose with GPU support, and
an NVIDIA driver compatible with the publisher's CUDA 13 runtime. The tested
RTX 5090 Laptop GPU has 24 GB VRAM; approximately 64 GB system RAM and at least
60 GB available Docker storage are recommended. The publisher image alone is
about 29 GB unpacked, and the original checkpoint is about 18.4 GB.

Accept the [model's access terms](https://huggingface.co/swiss-ai/Apertus-v1.5-8B)
using a Hugging Face account with read access. Neither the model weights nor
any access token is included in this repository or release.

From the repository root in PowerShell, create the ignored local configuration
only if you do not already have one:

```powershell
if (-not (Test-Path .\local\.env)) {
    Copy-Item .\local\.env.example .\local\.env
}
$config = docker compose --project-directory .\local --file .\local\compose.yaml config --format json | ConvertFrom-Json
$modelVolume = $config.volumes.models.name
$modelImage = $config.services.inference.image
docker volume create $modelVolume
docker compose --project-directory .\local --file .\local\compose.yaml pull inference
docker compose --project-directory .\local --file .\local\compose.yaml build frontend
```

For a new/empty volume, download the pinned snapshot once. The command prompts
for a read token inside the temporary container; it is not placed in process
arguments, environment variables, image layers, or a persistent login:

```powershell
$download = "import getpass; from huggingface_hub import snapshot_download; snapshot_download('swiss-ai/Apertus-v1.5-8B', revision='a411d838600baf0e3635a3daf66fb7c55fc97bb6', cache_dir='/models/hub', token=getpass.getpass('Hugging Face read token: '), max_workers=2)"
docker run --rm --interactive --tty --mount "type=volume,source=$modelVolume,target=/models" --entrypoint python $modelImage -c $download
```

Skip the download when reusing an existing complete cache. Do not copy model
files into the Git worktree. The network is needed for initial image/package
and model downloads, not for normal inference.

## Start and stop

Enable the RTX 5090 in the laptop's graphics-mode utility and connect AC power.
Windows must actually expose the NVIDIA device; an iGPU-only/Eco mode cannot
run this stack. Compose does not start either service until you request it.

From the repository root in PowerShell:

```powershell
docker compose --project-directory .\local --file .\local\compose.yaml up --detach --no-build --pull never --wait --wait-timeout 900
```

Open <http://localhost:8080>. Startup of the cached model can take 3-5 minutes.
Only the UI is published, bound to `127.0.0.1`. The unauthenticated
OpenAI-compatible vLLM API is private to the model's internal Docker network.

```powershell
docker compose --project-directory .\local --file .\local\compose.yaml stop
docker compose --project-directory .\local --file .\local\compose.yaml logs --tail 100
```

No background download or Azure login is needed at runtime. The external model
volume is never removed by `compose down`; do not run a volume prune against it.

## Local resources

- The pinned Swiss AI vLLM publisher image and the original
  `swiss-ai/Apertus-v1.5-8B` snapshot
  `a411d838600baf0e3635a3daf66fb7c55fc97bb6`.
- The `apertus-local-frontend:1.5` image.
- One existing Docker volume containing the verified model and runtime caches.
  The ignored `.env` selects this laptop's volume using `APERTUS_MODEL_VOLUME`.
  `.env.example` documents the setting; no credential belongs in that file.
- Two Compose services. There is no auto-start/restart policy; start the stack
  only when the NVIDIA GPU is available.

The cache must contain all six original safetensors files plus the
processor/tokenizer files. Use the first-time setup above on another machine.
Do not copy tokens into images or release assets.

## Tested profile and limits

- Native original weights; native BF16 KV, not the rejected FP8 configuration.
- One sequence, 16,384 total context tokens, 2.5 GiB explicit KV pool.
- 1,024-token prefill batches, batch-one decode-only CUDA graphs.
- `VLLM_USE_V2_MODEL_RUNNER=0` for the tested WSL compatibility issue.
- Zero CPU weight offload.
- Prior RTX 5090 Laptop measurements: approximately 33-35 generated tokens/s.
  These measure the backend with the same original model/runtime pins, not
  browser latency or a guarantee across driver versions and laptop power modes.

The explicit KV pool overrides automatic cache sizing: GPU utilization `0.90`
is not a hard overall memory ceiling. Text baseline peak was approximately
20.8 GiB; larger media probes reached approximately 22.6 GiB. Keep desktop
headroom and do not start a second model replica.

Text, image and audio input are enabled. Images are resized to fit 512 x 512.
Audio is decoded locally to 24 kHz mono PCM and limited to ten seconds.
At most one image and one audio file, 20 MiB each, are accepted per turn.
Context accounting reserves output and conservative expanded-media overhead,
and omits only oldest complete history pairs when necessary. Only text from
previous turns is retained; prior media is not re-sent.

**Known accuracy limitation:** fresh media probes passed, but adding unrelated
numbered text caused incorrect audio answers in the original runtime. This is
not fixed by the frontend. Verify important media answers. Output is text;
native video input, generated speech, and generated images are not supported.

The Chat and Thinking profiles stream final-answer text. Thinking does not
display hidden reasoning. Tools permits only the local calculator and clock;
tool selection is buffered before any result is displayed. Web Search is not
registered, and there is no fallback to another model provider.

## Rebuild or test

The frontend has its own dependency manifest and frozen lock under
`src\frontend\local`; cloud dependencies and their lock are unchanged.
The local build uses a public PyPI mirror because this laptop's connection to
`files.pythonhosted.org` failed TLS negotiation. TLS verification stays enabled,
and locked artifact hashes are checked against official PyPI release metadata
before the prepared image is accepted. No private code or credentials are sent
to the package index.

```powershell
docker compose --project-directory .\local --file .\local\compose.yaml build frontend
```

Local CPU-only tests live in `src\frontend\local\tests`. Run them in their own
environment, not in the cloud test process:

```powershell
uv sync --project .\src\frontend\local --frozen --dev
uv run --project .\src\frontend\local --frozen pytest -c .\src\frontend\local\pyproject.toml .\src\frontend\local\tests -q
```

They validate native
media request shapes, preprocessing limits, context budgets, local tools,
reasoning separation, streaming failures, and single-request admission without
requiring GPU weights or Azure access. Isolation checks also prove that ambient
Azure settings cannot activate clients and that the cloud configuration still
requires its services. Run the existing Azure frontend suite separately:

```powershell
uv run --project .\src\frontend --frozen pytest .\src\frontend\tests -q
```

Keep local `.env`, virtual environments, uploaded media, generated UI assets,
test caches, model weights, and Docker exports out of commits/releases.
