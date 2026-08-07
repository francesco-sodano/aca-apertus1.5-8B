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

Classifier severity, configured thresholds, prompts, evidence, and blocked
outputs are logged only as privacy-safe metadata where applicable; they are not
shown in the UI.

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
- no raw query, evidence, or answer content in telemetry;
- sanitized Foundry/APIM request identifiers and Web Search action counts for
  support and cost analysis.

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
Deployment opens a temporary authenticated build window and restores the private
posture on success and failure.

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

## Production Checklist

- Restrict the Entra application to approved users or groups.
- Review Content Safety thresholds against domain policy.
- Decide whether to disable image and audio uploads or add OCR/audio moderation.
- Verify private endpoint approval and DNS resolution.
- Run the live safety probe plus broader multilingual and multimodal red-team
  evaluations.
- Verify telemetry contains no prompts, evidence, output, credentials, or search
  queries.
- Assign incident ownership and validate budgets, alerts, Defender findings, and
  rollback procedures.
- Pin and review model, image, dependency, and GitHub Actions updates.