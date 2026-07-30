# Apertus frontend

Chainlit 2.11.1 frontend for the grounded Apertus deployment.

## Local setup

```powershell
uv sync --dev
uv run pytest
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
development, `az login` is usually sufficient. Content Safety remains mandatory.

## Native tools

In the Tools profile, Apertus natively selects Web Search, Calculator, Current
Time, or no tool. A small non-streaming selector request uses the vLLM Apertus
tool parser; the final answer remains streamed. The frontend executes at most
one selected tool, validates its arguments with JSON Schema, applies its timeout,
safety-screens the result, and rejects unknown or additional calls.

Built-ins are defined in [`apertus_frontend/tools.py`](apertus_frontend/tools.py):

| Name | Purpose |
| --- | --- |
| `search_web` | Foundry Web Search for current or explicitly verified public facts, with Prompt Shield and citations |
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
