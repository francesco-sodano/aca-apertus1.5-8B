# Security and Grounding

## Trust Boundaries

- The browser and uploaded files are untrusted.
- Retrieved web content is untrusted data, never instructions.
- Apertus output is untrusted until output policy checks finish.
- The public frontend requires Container Apps Easy Auth with a single-tenant
  Microsoft Entra app registration. External ingress is enabled only after the
  provider and callback URI are configured.
- The inference app is internal and also requires a randomly generated API key.

## Fail-Closed Policy

There is no application path to vLLM until required input checks succeed.
Requests involving changing information also require Foundry evidence and Prompt
Shields approval. If the preview Groundedness detector does not approve an
Apertus answer, the application retries once. A second inconclusive result uses
the cited, Prompt-Shielded, output-moderated Web Search summary instead of
discarding successful retrieval. Requests are still refused when no cited safe
fallback exists. Stable explanations, transformations, and writing tasks do not
pay the web-search latency.

Safety checks include:

- Analyze Text and Prompt Shields for user text.
- Analyze Image for every image.
- Prompt Shields for the web evidence packet.
- Analyze Text for generated output.
- Groundedness Detection for supported responses, with English as the reliable
  operating language for that preview API.

During processing, the UI reports the active safety, tool-selection, Web Search,
generation, and output-verification stage. A Content Safety rejection identifies
the blocked input or output, policy category, severity, and configured threshold.
Prompts, classifier payloads, and generated blocked content are not displayed or
logged with that diagnostic metadata.

Grounding uses Foundry Web Search, backed by Grounding with Bing, with low search
context and bounded `gpt-4.1-nano` output. Recent conversation turns resolve
follow-ups before retrieval. The model is instructed to answer from the
delimited evidence and user media. Exact Web Search URLs are returned when
available. This reduces unsupported claims but cannot prove that a neural model
never relies on learned weights.

Tool selection is owned by the frontend boundary. Obvious live or local intents
use deterministic rules; ambiguous factual requests use a structured semantic
classifier. A classifier failure defaults to Web Search. Apertus never receives
a search tool and therefore cannot override, duplicate, or contradict the
application's retrieval decision.

The separate Grounding with Bing Search agent tool is not used. New Bing
resources are suspended in this subscription, and benchmark calls through the
existing resource failed authentication after taking longer than the successful
Foundry Web Search path.

## Audio Exception

Azure AI Content Safety does not moderate raw audio in this design. Audio MIME
type, size, and presence are validated, then the bytes are passed directly to
Apertus. The UI warns the user. Disable audio if this exception is unacceptable.

## Identity and Secrets

The frontend system-assigned identity receives Foundry User and Content Safety
Cognitive Services User permissions and is selected implicitly by
`DefaultAzureCredential`. No identity client ID is injected into the container.
The frontend user-assigned identity receives only ACR pull and Key Vault secret
read for platform-managed image pulls and secret references. The inference
user-assigned identity also receives only ACR pull and Key Vault secret read.
The Foundry project identity receives Foundry User on its parent account. The
deployment principal receives Key Vault Secrets Officer to initialize secrets.

The Entra client secret is also a versionless Key Vault reference. Initial values
and explicit rotations enter through secure ARM parameters, allowing the vault
public endpoint to remain disabled from creation onward. Later deployments
preserve existing versions. Rotation requires
`ROTATE_APPLICATION_SECRETS=true` and fresh source credentials.

ACR admin access and Cognitive Services local keys are disabled. Secret values
are never included in images, Bicep outputs, or committed parameters. Key Vault
references use versionless URLs so Container Apps can pick up rotated versions.

The model cache is a Premium NFS Azure Files share reached through a storage
private endpoint. Shared-key authorization and public network access are both
disabled. NFS uses network-level authorization; access is limited to the private
VNet path. ACA environment mounts do not expose AZNFS TLS configuration, so the
cache relies on Private Link and Azure platform transport protection. Do not put
application secrets or user data in the model cache.

Key Vault, Foundry, and Content Safety also use private endpoints and linked
private DNS zones. ACR admin and anonymous access are disabled. ACR is private
and export-disabled at rest. Postprovision temporarily enables its authenticated
public build plane and export policy for Microsoft-hosted ACR Tasks and azd;
postdeploy disables both before enabling frontend ingress. Runtime pulls use
managed identity and Private Link. The interactive deployer receives `AcrPush`
only at registry scope; frontend and inference identities receive `AcrPull`.
Foundry project audit logs and metrics also flow to the tenant-managed MCAPSGov
diagnostic storage account, matching the inherited governance policy.

## Data Handling

Application logs contain correlation IDs, profile names, counts, dependency
status, policy decisions, latency, and error classes. They must not contain raw
prompts, attachments, evidence text, generated text, access tokens, or secrets.

Grounding with Bing can process search data outside Azure compliance and
geographic boundaries and outside the Microsoft DPA. Deployment requires an
explicit `ACCEPT_BING_GROUNDING_TERMS=true` acknowledgement.

## Residual Defender Recommendations

A fresh postdeployment policy scan reported no MCAPSGov noncompliance. Two
non-enforcing `ASC Default` recommendations remain:

- **Secure transfer to storage accounts should be enabled.** The Premium NFS
  model cache cannot use the global HTTPS-only switch with ACA's native NFS
  environment mount. The account has no shared-key or public access and is
  reachable only through its approved private endpoint. Do not store secrets or
  user data in this cache.
- **All Internet traffic should be routed via your deployed Azure Firewall
  (preview).** The deployment uses private endpoints and private DNS for service
  traffic but does not add a centralized Azure Firewall. Add an egress firewall
  only with a reviewed FQDN allowlist that preserves Hugging Face model download,
  Foundry Web Search, Entra authentication, and Azure Monitor connectivity.

## Production Checklist

- Restrict the Entra application to assigned users or groups and test WebSocket
  sign-in behavior.
- Review Content Safety thresholds against domain policy.
- Decide whether to disable audio.
- Verify private endpoint approval and DNS resolution from the ACA VNet.
- Configure budgets, alerts, Defender for Cloud, and incident ownership.
- Run adversarial prompt-injection, unsafe-media, and multilingual tests.
- Verify telemetry contains no content or credentials.
- Pin and review every model, image, and Python dependency update.