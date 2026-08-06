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
Requests involving changing information also require Web IQ evidence and Prompt
Shields approval. If the preview Groundedness detector does not approve an
Apertus answer, the application retries once. A second inconclusive result uses
the deterministic, cited, Prompt-Shielded, output-moderated source-excerpt
fallback instead of discarding successful retrieval. Requests are still refused
when no cited safe fallback exists. Stable explanations, transformations, and
writing tasks do not pay the web-search latency.

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

Grounding uses Microsoft Web IQ Web Search with strict SafeSearch and bounded
query-relevant passages. Recent conversation turns resolve follow-ups before
retrieval. Apertus is instructed to answer from the delimited passage evidence
and user media. Exact source URLs and Web IQ support trace IDs are retained.
This reduces unsupported claims but cannot prove that a neural model never
relies on learned weights.

Tool execution is owned by the frontend boundary, while Apertus owns the native
selection decision. Obvious local intents bypass selection. Other Tools-profile
requests receive one forced, registry-generated `select_tool` schema; explicit
current/future requests omit `none`. The frontend accepts at most one registered
tool, validates its JSON arguments, enforces its timeout, executes the
allowlisted handler, and safety-screens the result. Unknown, malformed, or extra
calls fail closed. Web Search additionally preserves citations and passes
Prompt Shield and groundedness controls.

## Audio Exception

Azure AI Content Safety does not moderate raw audio in this design. Audio MIME
type, size, and presence are validated, then the bytes are passed directly to
Apertus. The UI warns the user. Disable audio if this exception is unacceptable.

## Identity and Secrets

The frontend system-assigned identity receives Content Safety Cognitive Services
User permission and is selected implicitly by `DefaultAzureCredential`. No
identity client ID is injected into the container. Web IQ uses an API key stored
in Key Vault and exposed only through a Container Apps secret reference.
The frontend user-assigned identity receives only ACR pull and Key Vault secret
read for platform-managed image pulls and secret references. The inference
user-assigned identity also receives only ACR pull and Key Vault secret read.
The deployment principal receives Key Vault Secrets Officer to initialize
secrets.

The Entra client secret is also a versionless Key Vault reference. Initial values
and explicit rotations enter through secure ARM parameters, allowing the vault
public endpoint to remain disabled from creation onward. Later deployments
preserve existing versions. Full application rotation requires
`ROTATE_APPLICATION_SECRETS=true` and fresh source credentials. Web IQ can be
rotated independently with `ROTATE_WEBIQ_SECRET=true` and a replacement
`WEBIQ_API_KEY`.

ACR admin access and Cognitive Services local keys are disabled. Secret values
are never included in images, Bicep outputs, or committed parameters. Key Vault
references use versionless URLs so Container Apps can pick up rotated versions.
The vLLM API key remains environment-only and is not placed in process arguments
or startup command logs.

The model cache is a Premium NFS Azure Files share reached through a storage
private endpoint. Shared-key authorization and public network access are both
disabled. NFS uses network-level authorization; access is limited to the private
VNet path. ACA environment mounts do not expose AZNFS TLS configuration, so the
cache relies on Private Link and Azure platform transport protection. Do not put
application secrets or user data in the model cache.

Key Vault and Content Safety also use private endpoints and linked
private DNS zones. ACR admin and anonymous access are disabled. ACR is private
and export-disabled at rest. Postprovision temporarily enables its authenticated
public build plane and export policy for Microsoft-hosted ACR Tasks and azd;
postdeploy disables both before enabling frontend ingress. Runtime pulls use
managed identity and Private Link. The interactive deployer receives `AcrPush`
only at registry scope; frontend and inference identities receive `AcrPull`.
Web IQ request telemetry records only status, latency, result count, evidence
size, and the Web IQ trace ID; it never records the query, passages, or API key.

## Data Handling

Application logs contain correlation IDs, profile names, counts, dependency
status, policy decisions, latency, and error classes. They must not contain raw
prompts, attachments, evidence text, generated text, access tokens, or secrets.

Web IQ is an external public grounding API. Deployment requires an explicit
`ACCEPT_WEBIQ_TERMS=true` acknowledgement after the operator reviews the terms
associated with the enabled Web IQ profile.

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