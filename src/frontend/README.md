# Apertus frontend

Chainlit 2.12.0 frontend for the grounded Apertus deployment.
The image uses Python 3.13.15; CI tests both Python 3.12 and 3.13.
See [the code map](../../docs/architecture.md#code-map) for module ownership.
The [Dockerfile](Dockerfile) pins the base and build-tool images;
[uv.lock](uv.lock) records the tested Python dependency versions.

## Local setup

```powershell
uv sync --frozen --dev
uv run --frozen pytest
```

To run the UI, authenticate with Azure and set the required environment values:

```powershell
$env:MODEL_ENDPOINT = 'https://<vllm-host>/v1'
$env:MODEL_ID = 'swiss-ai/Apertus-v1.5-8B'
$env:VLLM_API_KEY = '<vllm-api-key>'
$env:CONTENT_SAFETY_ENDPOINT = 'https://<account>.cognitiveservices.azure.com'
$env:FOUNDRY_PROJECT_ENDPOINT = 'https://<account>.services.ai.azure.com/api/projects/<project>'
$env:FOUNDRY_GROUNDING_MODEL = 'gpt-4.1-nano-grounding'
$env:MODEL_HEALTH_TOKEN = '<operator-health-token>'
uv run chainlit run app.py --host 127.0.0.1 --port 8000
```

`DefaultAzureCredential` supplies Content Safety and Foundry tokens. For local
development, `az login` supplies credentials, but the identity also needs the
appropriate data-plane roles and network access to the private services. Content
Safety remains mandatory. Local Chainlit does not provide the Azure Easy Auth
sidecar, so bind only to localhost for local testing.

## Runtime Configuration

Set these in the frontend process/container, not in the browser. Most are read
by [settings.py](apertus_frontend/settings.py) when the runtime is initialized.

| Environment value | Default / meaning |
| --- | --- |
| `MODEL_ENDPOINT` | Required; OpenAI-compatible vLLM URL ending in `/v1` |
| `VLLM_API_KEY` | Required; internal vLLM credential, supplied through Key Vault in Azure |
| `CONTENT_SAFETY_ENDPOINT` | Required; Content Safety account endpoint |
| `FOUNDRY_PROJECT_ENDPOINT` | Required; Foundry URL ending in `/api/projects/<project>` |
| `MODEL_HEALTH_TOKEN` | Required; separate bearer credential for `/healthz/model` |
| `MODEL_ID` | `swiss-ai/Apertus-v1.5-8B` |
| `FOUNDRY_GROUNDING_MODEL` | `gpt-4.1-nano-grounding` deployment name |
| `CONTENT_SAFETY_THRESHOLD` | `4`; inclusive block threshold, validated from 0 to 7 |
| `MAX_OUTPUT_TOKENS` | `2048`; positive maximum for answer generation; selector uses 160 |
| `TEMPERATURE` | `0.2`; selector always uses 0 |
| `MODEL_TIMEOUT_SECONDS` | `900`; client timeout for vLLM calls |
| `GROUNDING_TIMEOUT_SECONDS` | `120`; Foundry client timeout; search tool also has a fixed 120-second broker timeout |
| `MAX_CONCURRENT_REQUESTS` | `4`; in-flight requests shared by all users per replica |
| `REQUESTS_PER_MINUTE` | `6`; per principal per replica |
| `ADMISSION_QUEUE_TIMEOUT_SECONDS` | `5`; wait for concurrency slot |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Optional locally, set by Bicep in Azure; initializes the Azure Monitor exporter |
| `OTEL_SERVICE_NAME` | Set to `apertus-frontend` by Bicep |
| `TRACE_CONTENT` | `false`; only case-insensitive `true` enables unredacted selected text/tool content |

Limits on message length, attachment counts/sizes, conversation history, and tool
arguments live in [pipeline.py](apertus_frontend/pipeline.py) and
[tools.py](apertus_frontend/tools.py). Chainlit additionally applies its upload
allowlist and 10 MB per-file limit from [config.toml](.chainlit/config.toml).
Changing only an azd environment value does not inject an arbitrary runtime
setting: use the container environment or explicitly wire it into Bicep.

## Native tools

In the Tools profile, Apertus natively selects Web Search, Calculator, Current
Time, or no tool. A small non-streaming selector request uses the vLLM Apertus
tool parser. vLLM's answer stream is buffered for output checks before display.
The frontend executes at most
one selected tool, validates its arguments with JSON Schema, applies its timeout,
safety-screens the result, and rejects unknown or additional calls.

Freshness/date/plot-pattern matches disallow `none`; local writing/explanation
requests can skip the selector. The `Thinking` profile uses the same model with
thinking enabled but no tool registry or automatic Web Search. Both profiles
apply the safety pipeline. Model refusal labeling is heuristic and should not
be treated as a machine-readable decision from Apertus.

The registry enforces a total of one handler call per request as well as each
tool's `max_calls`. A failed handler consumes its budget. Calls with invalid
schemas are rejected before execution. Missing or malformed Content Safety
responses fail closed rather than being accepted as a low score.

Built-ins are defined in [`apertus_frontend/tools.py`](apertus_frontend/tools.py):

| Name | Purpose |
| --- | --- |
| `search_web` | Foundry Web Search for current or explicitly verified public facts, with strict safe-result instructions, bounded evidence, Prompt Shield, Content Safety, groundedness checks, retries, citations, and support request IDs |
| `calculator` | Bounded AST-only arithmetic without Python `eval` or arbitrary execution |
| `get_current_time` | Current date and time for a validated IANA timezone |

To extend the registry, create an asynchronous handler returning `ToolResult`,
describe it with a `ToolSpec`, and pass the spec through
`GroundedCompletionService(additional_tools=(...))`. Use a unique function name,
a precise selection description, a closed JSON Schema with
`additionalProperties: false`, a short timeout, and a read-only handler. The
generated selector automatically includes the new name; no gateway routing
branch is required. Add argument-validation and selector tests alongside
[`tests/test_tools.py`](tests/test_tools.py) and
[`tests/test_vllm_gateway.py`](tests/test_vllm_gateway.py).

Raw audio is intentionally not moderated by Azure AI Content Safety. Disable
audio uploads if the target policy requires spoken-content moderation.

Images pass Content Safety harm-category analysis, but embedded text is not
OCRed and sent through Prompt Shield. Add that stage or disable image uploads
when untrusted screenshots or documents are in scope.

Safety blocks and refusals use one concise message that names the blocking
service and violated rule. Severity, threshold, correlation IDs, and detailed
classifier metadata remain telemetry-only.

## Tracing

Each Chainlit message starts an `apertus.chat` trace. The request pipeline,
selector/correction, application tool, Foundry call, model generation, safety,
groundedness, optional regeneration, and response delivery are nested spans.
Usage-only vLLM chunks supply actual token counts when available. Selector calls
are non-streamed and trace their own returned usage. Retry events include type
and delay, without copying dependency exception bodies.
The OpenAI 3 SDK uses native HTTPX2; Azure REST clients use HTTPX. SDK-wrapped
connection/timeouts are retried. Chainlit's `on_app_shutdown` callback closes
runtime clients and flushes pending traces before process exit.

For metadata tracing, set `APPLICATIONINSIGHTS_CONNECTION_STRING` before starting
the process. For unredacted text capture, additionally set:

```powershell
$env:TRACE_CONTENT = 'true'
```

Set it to `false` to disable content for subsequent runs. Content opt-in selects
user/assistant/tool text and arguments/results, never system/developer messages,
authentication fields, media payloads, or reasoning fields. There is no PII/secret
redaction of eligible text, restricted-table RBAC, or retention change.
See [operations and KQL](../../docs/operations.md#inference-tracing) and
[the content policy](../../docs/security-and-grounding.md#trace-content-policy).

The tracing tests use an in-memory exporter and require no Azure resources:

```powershell
uv run --frozen pytest
uv run --frozen python -m compileall app.py apertus_frontend safety_evaluation.py
```

## Safety Evaluation

Validate the static safety-case manifest without credentials, then run the same
two cases against configured Content Safety and Apertus services:

```powershell
uv run --frozen python safety_evaluation.py --validate-cases
uv run --frozen python safety_evaluation.py
```

The live probe prints only case IDs, blocker/rule metadata, and pass status. It
does not print the prompts or model answers.
Run it from this source checkout with the configured private-service access.
It uses `Thinking` mode and does not call Foundry or initialize an Azure Monitor
exporter. The CLI is also packaged into the frontend image and can be run as
`python safety_evaluation.py --validate-cases` there.

## Health and Sign-Out

- `/healthz`: process liveness.
- `/healthz/ready`: runtime configuration/client initialization, no dependency call.
- `/healthz/model`: bearer-protected check of vLLM `/health`, not a model completion.
- `/.auth/logout`: Azure Easy Auth sign-out, available only on the deployed host.
  There is currently no sign-out header control in Chainlit.

Conversation history is in memory and bounded to six turns. It can be lost on
replica change/restart; telemetry is not a replacement for persistent chat storage.

## Container Build

The frontend Dockerfile defaults to `https://pypi.org/simple`. Supply the
generic `PYTHON_PACKAGE_INDEX_URL` build argument only when another compatible
package index is required; do not commit private registry URLs.
The image runs as user 10001 on port 8000 and copies only runtime modules,
the safety CLI, Chainlit configuration, and public assets. Builds export exact
runtime requirements from the checked-in lock and require matching artifact
hashes when installing from the selected index. They do not refresh dependency
resolution. A mirror must supply those exact artifacts or the build fails.
Both build contexts exclude local `.env` files, key/certificate files, and
development state. Pin image digests and retain the preceding verified artifact
for rollback.
