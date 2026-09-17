# Security and Grounding

## Trust Boundaries

- Browser input and uploaded files are untrusted.
- Retrieved web evidence is untrusted data, never instructions.
- Apertus output is untrusted until output checks complete.
- The frontend requires single-tenant Entra authentication.
- Inference is internal-only and requires a generated API key.

## Safety Layers

The application combines independent controls:

| Layer | Responsibility |
| --- | --- |
| Apertus post-training | Learned model behavior, including refusals governed by the Apertus Charter |
| Prompt Shield | Detects direct attacks in user text and indirect attacks in retrieved evidence |
| Analyze Text | Moderates user text, tool output, and generated text |
| Analyze Image | Moderates uploaded image content for Content Safety categories |
| Groundedness Detection | Checks generated factual claims against approved evidence |

Apertus itself does not ship with a separate output filter. Its learned refusal
behavior is not the same as Azure AI Content Safety. The UI distinguishes them:

- `Your message was blocked by Azure AI Content Safety. Rule: <category>.`
- `Your message was blocked by Apertus. Rule: <category>.`
- Grounding failures name Foundry Web Search or Groundedness Detection.

The default text threshold is 4 on the eight-severity-level Analyze Text scale.
Azure can report a nonzero category below that threshold without blocking.
Prompt Shield is an independent attack-detection gate; image analysis uses its
four-level severity output. Service errors propagate instead of bypassing safety.
An HTTP 200 without an explicit Prompt Shield boolean or all four valid harm
category results is an error, not approval. A malformed groundedness value stays
indeterminate and follows the documented retry/fallback path.

Apertus does not return a machine-readable policy category. The application
labels a model refusal only when an English refusal prefix accompanies a
nonzero Azure input assessment or a harm keyword in the request. This is a
heuristic, not an authoritative model-policy decision: false positives and
missed refusals, especially in other languages, remain possible. Harmless
`I cannot...` limitations without that signal are preserved.

Severity and threshold stay out of visible block messages. By default telemetry
contains metadata only; optional text capture follows the policy below.

## Multimodal Limitations

Prompt Shield accepts text, not image pixels. Uploaded images pass Analyze Image,
which evaluates harm categories, but text embedded inside an image is **not OCRed
and sent through Prompt Shield**. A vision-capable model can therefore receive
instruction-like text that Prompt Shield did not inspect. Applications that
accept untrusted screenshots or documents should add OCR plus Prompt Shield, or
disable image uploads for higher-risk deployments.

Raw audio is the other explicit exception. MIME type, size, and presence are
validated, but spoken content is not moderated by Azure AI Content Safety before
it reaches Apertus. Disable audio where policy requires audio moderation.

## Foundry Web Search

The application uses the Foundry Responses API `web_search` tool with GPT-4.1
nano, a forced tool choice, and low search context. The public API does not expose
a SafeSearch field, so the request uses strict safe-result instructions and the
returned evidence is subsequently protected by Prompt Shield and Content Safety.

Controls include:

- one application Web Search call per request;
- 120-second timeout, bounded retries, and circuit breaking;
- evidence capped at 8,000 characters and five deduplicated web citations;
- no query, evidence, or answer content in telemetry by default;
- Foundry/APIM support request identifiers and Web Search action counts for
  support and cost analysis.

The one-call limit is the application's tool limit, not a guarantee of a single
Bing transaction: the service can perform multiple search actions and transient
retries can repeat a request. Groundedness recovery regenerates once, then can
return the cited, safety-screened Foundry summary without an affirmative
groundedness approval. That fallback is explicitly flagged in traces; it is not
an approved Apertus answer. Citation-free evidence can ground a model response,
but cannot be returned as the summary fallback.

Grounding with Bing is a First Party Consumption Service with separate terms and
transaction charges. Search data can leave the Azure compliance and geographic
boundary. Deployment requires `ACCEPT_BING_GROUNDING_TERMS=true`.

## Identity and Secrets

The frontend system identity receives resource-scoped Foundry User and Cognitive
Services User roles. User-assigned frontend and inference identities receive
only ACR pull and Key Vault secret-read roles. The deployer receives scoped ACR
push and Key Vault secret-write roles.

Secrets are stored in private, RBAC-enabled Key Vault and referenced with
versionless URLs. Bootstrap values are cleared from azd state after successful
initialization. The vLLM key is supplied through the environment rather than a
process argument, so it does not appear in startup command logs. It can rotate
independently with `ROTATE_VLLM_SECRET=true`.

ACR is private, default-deny, export-disabled, and admin-disabled at rest.
The template explicitly supplies `Deny` and an empty allowed-IP list so a later
provision does not reset the firewall to a module/platform default.
Full deployment opens a temporary authenticated build window. Postprovision
failures attempt to restore the private posture; postdeploy closes the window
after a successful frontend deployment. Process interruption or failure before
postdeploy still requires an operator to verify and restore it.

Sign-out is handled by the platform at `/.auth/logout`. The Chainlit UI currently
does not include a sign-out control. Identity-provider cookies/SSO can affect a
later sign-in; this route is not a guarantee of sign-out from every Microsoft app.

## Trace Content Policy

`TRACE_CONTENT` is false by default. Only a case-insensitive `true` enables
content capture in the frontend process. With it disabled, custom tracing emits
operation names, timing, token counts, selected tool names, response/support IDs,
error types, and safety/grounding outcomes, not message bodies.

With it enabled, custom spans include:

| Attribute | Captured value |
| --- | --- |
| `gen_ai.input.messages` | User/assistant text, recent text history, and tool messages actually supplied to the operation |
| `gen_ai.output.messages` | Visible model/selector text and tool calls, bounded Foundry summary, and the final displayed answer/error |
| `gen_ai.tool.call.arguments` | Selected tool arguments |
| `gen_ai.tool.call.result` | Tool output, including retrieved evidence |

The application adds **no redaction** to these fields and no shorter retention
or protected-table/RBAC policy. Existing Azure telemetry permissions determine
who can see the content. A secret or personal information pasted into a prompt,
or returned in tool/model text, is captured as-is. Opt-in can capture generated
text or tool output later blocked by safety; logging does not mean it was
approved for display.

The following are excluded by field selection in both modes:

- System/developer messages and application system-prompt templates.
- Authentication configuration, API keys, tokens, and HTTP authentication headers.
- Image/audio bytes, media URLs/parts, and uploaded attachment payloads.
- Dedicated hidden reasoning fields from the model.
- Dependency exception bodies and stack traces; custom errors retain type and
  known policy metadata only.

These exclusions are not a content scanner. They cannot remove secrets or
instructions repeated inside otherwise eligible user/model/tool text. Regular
application logs remain metadata-only even when span content is enabled.
See [operations](operations.md#inference-tracing) for Azure storage behavior,
sampling/size limitations, and configuration commands.

## Security Evaluation

Run the deterministic manifest check in every CI build:

```powershell
uv run --project src/frontend --frozen python src/frontend/safety_evaluation.py --validate-cases
```

Run the live probe from a configured environment before release:

```powershell
uv run --project src/frontend --frozen python src/frontend/safety_evaluation.py
```

The probe tests an Azure Content Safety block and an Apertus refusal. It outputs
only case IDs, expected/actual blockers, rules, and pass status. It never prints
the test prompts or model answers. This is a small release gate, not a substitute
for a broader PyRIT or Foundry Risk and Safety evaluation campaign.

The CLI requires `CONTENT_SAFETY_ENDPOINT`, `MODEL_ENDPOINT`, `VLLM_API_KEY`,
Azure credentials with Content Safety data access, and network access to both
private endpoints. It runs in `Thinking` mode with no Web Search, and uses the
same safety pipeline. Classifier/model behavior can change; case expectations
are regression baselines, not permanent guarantees. Keep `TRACE_CONTENT=false`
when evaluating cases with a separately configured exporter. The standalone
CLI does not initialize the application's Azure Monitor exporter.

## Production Checklist

- Restrict the Entra application to approved users or groups.
- Review Content Safety thresholds against domain policy.
- Decide whether to disable image and audio uploads or add OCR/audio moderation.
- Verify private endpoint approval and DNS resolution.
- Run the live safety probe plus broader multilingual and multimodal red-team
  evaluations.
- Confirm the intended `TRACE_CONTENT` setting and validate the field exclusions.
  With content enabled, account for unredacted input/output under existing
  telemetry permissions and retention.
- Assign incident ownership and validate budgets, alerts, Defender findings, and
  rollback procedures.
- Pin and review model, image, dependency, and GitHub Actions updates.