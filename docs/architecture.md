# Architecture

## Resource Graph

The Bicep deployment creates one Sweden Central resource group containing:

| Resource | Role |
| --- | --- |
| Container Apps environment | Consumption and `Consumption-GPU-NC24-A100` workload profiles |
| Inference Container App | Internal vLLM endpoint, zero to one A100 replica |
| Frontend Container App | Public Chainlit HTTPS/WebSocket endpoint, one to three replicas |
| Premium ACR | Remote builds and mandatory inference artifact streaming |
| Storage account and Azure Files | Persistent Hugging Face and vLLM caches |
| Key Vault | Web IQ key, Hugging Face token, vLLM API key, operator health token, and Entra client secret |
| Managed identities | Frontend system identity for SDK calls; separate user identities for ACR and Key Vault |
| Microsoft Web IQ | External GA passage-search API used directly by the frontend |
| Content Safety account | Text, image, Prompt Shields, and groundedness checks |
| Log Analytics and Application Insights | Platform logs, application telemetry, and dependencies |
| VNet and dedicated subnets | ACA infrastructure and isolated private endpoints |
| Private DNS zones and endpoints | Private resolution for ACR, Storage, Key Vault, and Content Safety |

Azure Verified Modules create the shared services. Small local modules define
the two Container Apps because the GPU resource, volume mount, and probe
contracts need to remain visible and reviewable together.

Names scoped to the resource group derive from the azd environment. Globally
unique ACR, Storage, Key Vault, and Content Safety names additionally
include a deterministic suffix from the subscription, resource group, and
environment. Operators can supply validated name overrides through azd without
changing Bicep.

## Network Flow

The managed environment is injected into a dedicated `/23` infrastructure
subnet. The frontend remains the only public application endpoint and requires
Easy Auth. Inference ingress is `external: false`; its FQDN resolves and routes
only inside the Container Apps environment.

A separate `/24` subnet contains private endpoints. Private DNS zones linked to
the VNet resolve ACR, Azure Files, Key Vault, and Content Safety to
private IP addresses. Premium Azure Files uses NFS so the application does not
need a Storage account key. Storage shared-key authorization and its public
endpoint are disabled. ACR retains public control-plane access for ACR Tasks
while managed-identity image pulls use Private Link. Web IQ is reached through
its public HTTPS endpoint using a Key Vault-backed API key.

## Request Flow

1. The frontend validates the message and attachments.
2. Content Safety checks text, images, and prompt attacks. Raw audio bypasses
   content moderation by explicit design.
3. High-confidence local requests bypass tools. Other Tools-profile requests
   use a forced, registry-generated native `select_tool` function. Apertus
   chooses Web Search, Calculator, Current Time, another registered tool, or
   `none`; explicit current/future requests cannot choose `none`.
4. The frontend validates the selected name and JSON arguments, enforces one
   tool call and its timeout, executes the allowlisted handler, and screens the
   result with Content Safety.
5. Web Search evidence also passes Prompt Shield. Source URLs are required for
   the cited fallback path.
6. Apertus receives the tool result, recent turns, media, and profile settings
   on the one vLLM endpoint and streams the final answer.
7. Generated text passes Content Safety and, when grounded, Groundedness
   Detection before browser output.

## Scale and Startup

The frontend keeps one replica available and scales to three at four concurrent
HTTP requests. Inference scales from zero to one at one concurrent request and
uses a 30-minute cooldown. Startup probes allow 15 minutes for image streaming,
model download, and initialization. The model and compile caches survive replica
scale-down on Azure Files.

The default serving context is 32,768 tokens. Apertus supports up to 262,144,
but increasing context consumes KV cache and reduces concurrency on one A100.