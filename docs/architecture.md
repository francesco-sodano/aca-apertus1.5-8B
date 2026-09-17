# Architecture

The [root README](../README.md) describes installation. This guide maps the
current source code; [operations](operations.md) covers runtime settings,
deployment checks, and tracing queries.

## Code Map

| Source | Responsibility |
| --- | --- |
| [app.py](../src/frontend/app.py) | Chainlit callbacks, conversation history, health routes, admission, and delivery of checked responses |
| [pipeline.py](../src/frontend/apertus_frontend/pipeline.py) | Input limits, routing hints, tool broker, output safety, groundedness, retry generation, and refusal labeling |
| [vllm_gateway.py](../src/frontend/apertus_frontend/vllm_gateway.py) | OpenAI-compatible model calls, native selector parsing, prompt construction, stream collection, and model usage tracing |
| [tools.py](../src/frontend/apertus_frontend/tools.py) | Tool schemas, call limits/timeouts, AST-only calculator, current-time tool, and search tool specification |
| [azure_services.py](../src/frontend/apertus_frontend/azure_services.py) | Managed-identity Content Safety REST clients and Foundry Responses Web Search |
| [resilience.py](../src/frontend/apertus_frontend/resilience.py) | Transient retries, circuit breakers, and per-replica admission |
| [tracing.py](../src/frontend/apertus_frontend/tracing.py) | Span scopes, per-message trace isolation, opt-in text fields, error types, and usage metadata |
| [settings.py](../src/frontend/apertus_frontend/settings.py) | Required endpoints/secrets and runtime defaults |
| [config.toml](../src/frontend/.chainlit/config.toml) | Chainlit UI settings, upload allowlist, header link, and custom CSS; profiles/starters are registered in app.py |
| [safety_evaluation.py](../src/frontend/safety_evaluation.py) | Packaged and source-checkout safety regression CLI with metadata-only console results |
| [inference entrypoint](../src/inference/docker-entrypoint.sh) | vLLM model revision, runtime flags, and secret-free dry-run command |
| [main.bicep](../infra/main.bicep) | Azure services, identities, RBAC, private networking, caches, monitoring, and alerts |
| [azure.yaml](../azure.yaml) | Frontend azd service plus preprovision/postprovision/postdeploy hooks |
| [CI](../.github/workflows/ci.yml) | Frozen Python 3.12/3.13 tests, compilation, safety-case validation, Bicep, hook tests, and Dockerfile checks |

## Resource Graph

The Bicep deployment creates these resources in Sweden Central:

| Resource | Role |
| --- | --- |
| Container Apps environment | VNet-injected CPU and `Consumption-GPU-NC24-A100` workloads |
| Frontend Container App | Entra-authenticated Chainlit and FastAPI application |
| Inference Container App | Internal-only Swiss AI vLLM endpoint serving Apertus 1.5 8B |
| Microsoft Foundry account and project | Hosts the GPT-4.1 nano deployment used by Web Search |
| Azure AI Content Safety | Prompt Shield, text/image moderation, and groundedness detection |
| Premium ACR | Private frontend and inference images with artifact streaming |
| Premium Azure Files NFS | Persistent Hugging Face, vLLM, and compilation caches |
| Key Vault | Hugging Face, vLLM, health, and Entra secrets |
| Application Insights and Log Analytics | Application telemetry and Container Apps logs |
| VNet, private DNS, and private endpoints | Private access to ACR, Azure Files, Key Vault, Foundry, and Content Safety |

Globally unique names derive from the subscription, resource group, and azd
environment. Validated azd values can preserve or override generated names
without editing Bicep.

## Network Flow

The Container Apps environment uses a dedicated `/23` infrastructure subnet.
The frontend is the only public endpoint and is protected by Container Apps
built-in Entra authentication. The inference app uses internal ingress and an
API key stored in Key Vault.

A `/24` private-endpoint subnet and linked private DNS zones provide private
resolution for ACR, Azure Files, Key Vault, Foundry, and Content Safety. The
frontend authenticates to Foundry and Content Safety with its system-assigned
identity. User-assigned identities are used only for platform-managed ACR pulls
and Key Vault references.

Foundry Web Search uses Grounding with Bing. Search queries and tool parameters
leave the Azure compliance and geographic boundary under the Grounding with Bing
terms. Retrieved raw web content is not exposed directly to the application;
the Responses API returns a synthesized evidence packet and citations.

## Request Flow

The `Tools` profile follows this flow. `Thinking` enables the model's thinking
template and skips tool selection/execution; input and output safety still run.

1. The frontend creates a trace/correlation ID per chat message, loads bounded
   conversation history, applies per-replica admission, and validates limits.
2. User text passes Prompt Shield and Analyze Text. Images pass Analyze Image.
   Raw audio follows the documented moderation exception.
3. High-confidence local tasks bypass tools. Otherwise Apertus receives a
   forced registry-generated selector and chooses one allowlisted tool or
   `none`. Freshness/date/plot-pattern matches require a tool, while Apertus
   selects its name. Invalid selector arguments get one correction attempt.
4. The broker validates JSON Schema arguments, enforces a timeout and one-call
   limit, executes the selected handler, and moderates its output.
5. Web Search makes one logical forced, low-context Responses API call to Foundry.
   Transient retries and multiple service-side search actions are possible.
   The returned summary is capped at 8,000 characters and citations at five.
6. Retrieved evidence passes Prompt Shield before Apertus receives it.
7. Apertus generates the answer from the approved tool result and recent
   conversation context. vLLM chunks are buffered; only the checked answer is
   subsequently streamed to the browser.
8. Generated text passes Analyze Text. Grounded answers also pass Groundedness
   Detection. A failed or indeterminate groundedness decision gets one bounded
   regeneration; when that still fails, only the cited, safety-screened Foundry
   summary can be returned.
9. Azure blocks name the service and rule. Model refusal labeling is a heuristic
    using a refusal prefix plus a nonzero input safety assessment or harm keyword;
    ordinary inability statements without that signal are not relabeled.
10. The final displayed answer, including its source list, has a delivery span.
      Trace content is absent by default; the opt-in policy is described below.

## Trace Structure

```text
apertus.chat                          one SERVER span per Chainlit message
   apertus.request                     application pipeline / invoke_agent
      content_safety.input
         content_safety.text             category and threshold outcome
            content_safety.shieldPrompt   direct prompt check
            content_safety.analyze        text classification
         content_safety.image            only when an image is present
            content_safety.analyze        image classification
      apertus.tool_selection            only when tools are offered
         apertus.selector                one non-streamed model call
         apertus.selector                optional selector correction
      tool.execute                      selected application tool
         foundry.web_search              only for search_web
         content_safety.grounding
            content_safety.shieldPrompt   retrieved-evidence check
         content_safety.text             tool-output classification
      apertus.generate                  full streamed model response
      apertus.validate_output
         content_safety.output
            content_safety.text
         groundedness.detect             only with grounding sources
            content_safety.detectGroundedness
      apertus.regenerate                optional groundedness recovery
         apertus.generate
      apertus.validate_output           optional recovery validation
   apertus.respond                     final answer or user-facing error
```

The tree varies by profile, selected tool, attachments, and outcome. Each chat
message starts a fresh trace rather than inheriting a long-lived WebSocket
trace. Child spans and logs share `operation_Id`; use that ID for cross-stage
joins. The application `apertus.correlation_id` is attached to chat/request and
selection spans, not duplicated onto every dependency.

Spans use OpenTelemetry GenAI attributes for agent invocation, model calls,
tool execution, model/response IDs, finish reasons, and actual token usage.
Retry counts/events and circuit state stay on the dependency being retried.
No tracing SDK is installed inside vLLM: model spans measure calls from the
frontend, not internal GPU execution. The existing Azure Monitor exporter is
initialized only when `APPLICATIONINSIGHTS_CONNECTION_STRING` is present.

`TRACE_CONTENT=true` adds selected text and tool fields. System/developer
messages, authentication fields, media payloads, and reasoning fields are never
selected. Opted-in text is not redacted. No telemetry table RBAC or retention
settings are changed. See [the full policy](security-and-grounding.md#trace-content-policy).

## Scale and State

The frontend keeps one replica available and scales to three at four concurrent
HTTP requests. Sticky sessions improve conversation and in-process admission
consistency. The six-requests-per-minute principal limit and four-request
concurrency gate are **per frontend replica**, not global distributed quotas.
The concurrency gate is shared across users. Conversation state is in memory,
with at most six recent turns supplied to the model; it is not a durable chat
archive and can be lost on restart or a replica change.

Inference scales from zero to one at one concurrent request and uses a 30-minute
cooldown. Startup probes allow 15 minutes for artifact streaming, model loading,
and graph initialization. Model and compilation caches survive scale-down on
Premium Azure Files.

The serving context defaults to 32,768 tokens. Apertus supports up to 262,144,
but increasing context consumes KV cache and reduces concurrency on one A100.

The frontend pipeline accepts up to 32,000 text characters, four images at 4 MiB
each, and one audio file at 20 MiB. Chainlit separately limits uploads to five
files at 10 MB each, so browser audio uploads have that smaller UI limit. MIME,
base64, and size checks are not proof that the pinned model supports every media
format. Review [multimodal limitations](security-and-grounding.md#multimodal-limitations).