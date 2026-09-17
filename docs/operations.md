# Operations

Use the [root README](../README.md) for installation, the
[architecture guide](architecture.md) for code ownership, and the
[frontend guide](../src/frontend/README.md#runtime-configuration) for runtime
environment variables. Commands below assume the intended azd environment is
selected. Never print or export all azd values when diagnosing a deployment.

## Deployment Gates

Before `azd up`, verify:

1. Sweden Central supports `Consumption-GPU-NC24-A100` and the subscription has
   one available GPU replica.
2. GPT-4.1 nano Global Standard capacity is available.
3. The exact Apertus Hugging Face revision is pinned and its terms are accepted.
4. Grounding with Bing terms are accepted for the workload.
5. The Entra application is single tenant and the deployer can update redirects.
6. Alert email and monthly budget match operating policy.
7. Private endpoints, identities, role assignments, and disabled local/shared
   key authentication are permitted by policy.

The preprovision hook validates location, names, required values, terms flags,
the GPU profile, and artifact-streaming CLI support. Capacity and fleet driver
state still require live verification.

## Image Promotion

Inference promotion is transactional:

1. Verify `AcrPull` propagation for both workload identities.
2. Open the authenticated ACR build window.
3. Build or reuse the inference image tagged by azd environment and committed
   `src/inference` tree hash.
4. Enable artifact streaming and require conversion status `Succeeded`.
5. Configure versionless Key Vault references and Entra authentication.
6. Promote the inference image only after conversion succeeds.
7. Deploy the frontend with azd.
8. Restore ACR to private/default-deny/export-disabled and enable authenticated
   frontend ingress.

Failed, canceled, or timed-out conversion never updates inference. Hook failures
inside postprovision attempt ACR restoration. An interrupted process or failed
frontend publish before postdeploy can leave the window open; always check it
and follow [recovery](../README.md#recover-from-an-interrupted-deployment).

For frontend-only updates, `azd package --no-prompt` builds locally and
`azd deploy frontend --no-prompt` publishes to the existing app. These commands
do not run the postprovision hook. A public workstation cannot publish to the
private/default-deny ACR without an approved temporary build window or private
network access. A firewall 403 is not a reason to enable the ACR admin account.
ACR Tasks can build the frontend from the same Dockerfile when an approved build
window is used; promote only a verified image and close the window in a `finally`
block even if the build or promotion fails.

Run azd commands against one environment sequentially: package, provision, and
deploy share its environment-state lock. The frontend Dockerfile exports the
checked-in lock and uses hash-required installation from the configured index;
it never re-resolves versions during a build. Local/CI and deployment therefore
use the same selected versions, with platform-specific wheels from the lock.
Mirrors must supply matching artifacts. Pin the deployed image digest and retain
the preceding artifact. An alternate package index must provide the versions
recorded in [uv.lock](../src/frontend/uv.lock); do not disable TLS verification
or hash checks to work around missing packages.

## Health and Readiness

| Route | Purpose |
| --- | --- |
| Frontend `/healthz` | Startup and liveness |
| Frontend `/healthz/ready` | Validates runtime configuration and initializes clients without contacting the dependencies or waking the GPU |
| Frontend `/healthz/model` | Bearer-token-protected GET to vLLM `/health`; may wake the GPU, but does not generate an answer or verify Foundry/Content Safety |
| Inference `/health` | vLLM startup, readiness, and liveness |

Inference can take several minutes to load the model and capture graphs after
scale-up. The first user request can pay this cold-start cost when prewarm is not
available.

After deployment, verify health, the new revision's image/readiness/100% traffic,
Entra redirect behavior, private ACR posture, and one real chat/tool request.
The health routes alone are not proof that inference, search, and moderation work.

## Authentication and Admission

Container Apps Easy Auth redirects unauthenticated users to Entra ID. Public
health routes remain available for platform probes; `/healthz/model` requires a
separate bearer token.

To sign out, navigate in the browser to `/.auth/logout` on the frontend host.
Easy Auth clears the app session and performs the provider sign-out flow. Leave
the default logout-complete destination in place rather than redirecting directly
to the protected home page. The frontend does not currently expose a sign-out
button. Local Chainlit execution does not include Azure's Easy Auth sidecar.

Each frontend replica permits four in-flight requests and six requests per
authenticated principal per minute. Sticky sessions improve consistency, but
these are in-memory **per-replica** limits. Use a shared distributed limiter if
the deployment needs a strict global quota across three frontend replicas.

## Secret Rotation

Normal deployments preserve existing Key Vault versions.

- Full rotation: set fresh `HF_TOKEN` and `ENTRA_CLIENT_SECRET`, then set
  `ROTATE_APPLICATION_SECRETS=true` and run `azd up`.
- vLLM-only rotation: set `ROTATE_VLLM_SECRET=true` and run `azd up`.

Successful hooks clear source values and reset rotation flags. Remove superseded
Entra credentials only after application verification.

## Monitoring

Container logs are stored in Log Analytics. Frontend OpenTelemetry is exported
to the existing workspace-based Application Insights resource. Bicep configures
`APPLICATIONINSIGHTS_CONNECTION_STRING` and `OTEL_SERVICE_NAME=apertus-frontend`.
No new observability resource is needed for inference tracing.

The legacy events remain: `completion_allowed`, `tool_selected`, `tool_completed`,
`tool_rejected`, `request_blocked`, `grounding_rejected`, `request_failed`,
`groundedness_fallback_used`, and `web_search_completed`. Their metadata is stored
in `customDimensions.custom_dimensions` as a stringified dictionary. New span
attributes are directly addressable in `customDimensions` and logs emitted
inside the traced message share its `operation_Id`. Old records with an empty or
zero operation ID cannot be retroactively connected.

`completion_allowed` means the application pipeline finished. It can include a
model refusal or search-summary fallback, not necessarily an ordinary answer.
`tool_completed` means tool execution and output screening succeeded. A user
message can create a selector call, a correction, an answer call, and a
regeneration, so request count and inference count are different measures.

## Inference Tracing

### Configuration

Metadata tracing is active when the frontend starts with a valid
`APPLICATIONINSIGHTS_CONNECTION_STRING`. Without an exporter, OpenTelemetry
operations are no-ops. This does not affect chat behavior. The `enable_telemetry`
setting in Chainlit's configuration does not control these application spans.

The application explicitly configures `sampling_ratio=1.0` so the Azure Monitor
distro's default span-rate limit does not leave incomplete request trees. Standard
`OTEL_TRACES_SAMPLER` environment overrides can take precedence; leave them unset
for complete capture, and verify the Application Insights ingestion sampling is
also 100%. Full sampling increases telemetry volume, not content capture.

For local debugging, set environment values before starting Chainlit:

```powershell
$env:APPLICATIONINSIGHTS_CONNECTION_STRING = '<existing-resource-connection-string>'
$env:OTEL_SERVICE_NAME = 'apertus-frontend'
$env:TRACE_CONTENT = 'false'
```

To opt into unredacted text capture on the **existing Azure frontend** after the
tracing code is deployed:

```powershell
$resourceGroup = azd env get-value AZURE_RESOURCE_GROUP
$frontendName = azd env get-value SERVICE_FRONTEND_NAME
az containerapp update --resource-group $resourceGroup --name $frontendName `
  --set-env-vars TRACE_CONTENT=true --output none
```

Use the same command with `TRACE_CONTENT=false` to disable capture for the new
revision. This does not erase already-ingested content. `TRACE_CONTENT` is a
frontend runtime setting, not a Bicep/azd parameter: `azd env set TRACE_CONTENT`
alone does not forward it into the container, and a later full provision can
replace manual environment changes. The source default is false.

No redaction, shorter retention, or protected-table/RBAC configuration is added.
Anyone with the relevant existing telemetry access can view captured text,
including personal data or secrets present in user/model/tool text. Application
authentication fields, system/developer messages, attachments, and reasoning
fields are excluded. Review [the exact policy](security-and-grounding.md#trace-content-policy).

### Recorded Spans

See [the trace tree](architecture.md#trace-structure) for parenting. Main fields:

| Span | Key attributes |
| --- | --- |
| `apertus.chat` | `apertus.correlation_id`, `apertus.profile`, delivery/rejection outcome; one root per message, independent of the WebSocket lifetime |
| `apertus.request` | Routing hint, selected tools, citation count, attachment count, `apertus.trace_content`, groundedness result, regeneration count, fallback/refusal flags |
| `apertus.tool_selection` | Offered tool names, required/optional flag, `apertus.selected_tool` (including `none`), correction count |
| `apertus.selector`, `apertus.generate` | `gen_ai.operation.name=chat`, `gen_ai.provider.name=vllm`, request/response model, response ID, returned token usage and finish reasons |
| `apertus.generate` | `apertus.time_to_first_content_ms`, including network/retry time before the first visible content chunk; not time to first hidden reasoning token |
| `tool.execute` | `gen_ai.operation.name=execute_tool`, `gen_ai.tool.name`, call ID, citations, execution outcome |
| `foundry.web_search` | Azure OpenAI provider, model/response IDs, returned usage/status, support IDs, Web Search call/action counts, evidence size and citations |
| `content_safety.*` | Stage, API operation, category/severity/threshold or prompt-attack decision, HTTP status and outcome; never copies the moderation request body |
| `groundedness.detect` | Source count and `grounded`, `ungrounded`, or `indeterminate` result |
| `apertus.respond` | Final displayed message with source list when content is enabled; `apertus.delivery_status=sent` only after delivery finishes |

Model and safety dependencies record `apertus.attempt_count`,
`apertus.retry_count`, circuit state, and `retry` events with failure type and
backoff duration. There is one span per logical model operation; attempts are
represented by counters/events, not separate GPU/server traces. Custom span
errors record exception type and known policy fields without dependency error
bodies or stack traces. Handled failures also mark the root span.

### Find Requests

Open the application's **Application Insights resource > Logs** and select KQL
mode. These examples use the Application Insights table names:

```kusto
requests
| where timestamp > ago(24h)
| where name == "apertus.chat"
| project timestamp, trace_id=operation_Id,
   correlation_id=tostring(customDimensions["apertus.correlation_id"]),
   profile=tostring(customDimensions["apertus.profile"]),
   outcome=tostring(customDimensions["apertus.outcome"]), duration, success
| order by timestamp desc
```

Select a trace in Transaction Search/end-to-end transaction details to inspect
the hierarchy, or paste its `trace_id` here:

```kusto
let selectedTrace = "<trace-id>";
union requests, dependencies
| where operation_Id == selectedTrace
| project timestamp, id, parent=operation_ParentId, name, duration,
   success, customDimensions
| order by timestamp asc
```

Use `traces | where operation_Id == "<trace-id>"` for correlated log events.
When querying the Log Analytics workspace directly, the equivalent tables are
`AppRequests`, `AppDependencies`, and `AppTraces`, with `TimeGenerated`,
`OperationId`, and `Properties` in place of `timestamp`, `operation_Id`, and
`customDimensions`.

### Model and Tool Usage

```kusto
dependencies
| where timestamp > ago(24h)
| where tostring(customDimensions["gen_ai.operation.name"]) == "chat"
| extend model=tostring(customDimensions["gen_ai.request.model"]),
   inputTokens=tolong(customDimensions["gen_ai.usage.input_tokens"]),
   outputTokens=tolong(customDimensions["gen_ai.usage.output_tokens"])
| summarize calls=count(),
   callsWithUsage=countif(isnotnull(inputTokens) and isnotnull(outputTokens)),
   reportedInputTokens=sum(inputTokens), reportedOutputTokens=sum(outputTokens)
   by name, model
```

```kusto
dependencies
| where timestamp > ago(24h) and name == "tool.execute"
| extend tool=tostring(customDimensions["gen_ai.tool.name"])
| summarize executions=count(), failures=countif(success == false) by tool
```

Usage is recorded only when returned by the API. The frontend requests vLLM
streaming usage and reads the final usage-only chunk; absent usage remains
absent, not an estimated zero. Totals above cover only reported usage. Retries,
sampling, failures, and unavailable usage mean these are not billing totals.
Self-hosted Apertus token counts do not directly translate to a token-price bill.

### Read Captured Content

Azure routes recognized `gen_ai.input.messages`, `gen_ai.output.messages`,
`gen_ai.tool.call.arguments`, and `gen_ai.tool.call.result` attributes to
`AppGenAIContent` in the Log Analytics workspace (`genAIContent` in Application
Insights). Run this query in the linked **Log Analytics workspace > Logs**:

```kusto
AppGenAIContent
| where TimeGenerated > ago(24h) and TraceId == "<trace-id>"
| project TimeGenerated, TraceId, SpanId, ParentSpanId, ModelName,
   InputMessages, OutputMessages, ToolCallArguments, ToolCallResult
| order by TimeGenerated asc
```

No rows are expected before deployment/content opt-in and ingestion. The table
may not exist until content is first ingested. Per Microsoft's documented
migration, before September 30, 2026 these attributes are also routed to the
existing dependency/trace/event tables; after that date newly ingested content
values reside in the dedicated table. Do not assume content exists only in one
table or that turning capture off deletes older copies.

References: [GenAI telemetry data model](https://learn.microsoft.com/azure/azure-monitor/app/data-model-complete#generative-ai-telemetry)
and [AppGenAIContent columns](https://learn.microsoft.com/azure/azure-monitor/reference/tables/appgenaicontent).

### Retention and Limitations

This feature does not change retention. Bicep configures a 30-day workspace
default; individual Application Insights/content tables can have different
effective retention. Check the workspace's **Tables** settings for actual values.

Tracing applies to new executions only and is not a durable conversation archive.
OpenTelemetry sampling, exporter/ingestion failures, and backend field-size limits
can omit or truncate data. No application-level content redaction or truncation
is added, but existing request/evidence bounds still apply. A failed stream may
have no recorded final answer or usage. The trace records the client's observed
operations, not internal vLLM kernels, KV-cache state, hidden reasoning, or a
complete server-side Foundry search execution graph.

The Application Insights transaction view is the baseline viewer. Additional
Agents/Foundry views depend on the Azure portal's supported semantic conventions
and connecting the existing resource; this code does not provision that linkage.

## Rollback

Record the old revision and exact image before deploying. Verify the rollback
image exists; inference images additionally require successful artifact-stream
conversion. Activate a retained revision when available, or redeploy the prior
verified image. Single-revision mode should not be treated as durable revision
history. Never promote an unverified inference image or leave ACR public.

## Cost

The largest variable cost is the A100 replica while active. Persistent charges
include Premium ACR, Premium NFS Azure Files, private endpoints, telemetry,
Key Vault operations, Content Safety, Foundry model tokens, and Grounding with
Bing search transactions. Search action counts are logged so repeated Bing calls
can be measured before changing the grounding model.

## Cleanup

```powershell
azd down --purge
```

Confirm the selected azd environment and resource group first. `--purge`
permanently removes soft-deleted Key Vault data. Delete the separately created
Entra application only after Azure resource cleanup succeeds.