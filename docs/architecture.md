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
| Key Vault | Hugging Face token, vLLM API key, and operator health token |
| Managed identities | Frontend system identity for SDK calls; separate user identities for ACR and Key Vault |
| Foundry account and project | Adaptive Bing-backed Web Search and `gpt-5-mini` grounding deployment |
| Content Safety account | Text, image, Prompt Shields, and groundedness checks |
| Log Analytics and Application Insights | Platform logs, application telemetry, and dependencies |
| VNet and dedicated subnets | ACA infrastructure and isolated private endpoints |
| Private DNS zones and endpoints | Private resolution for ACR, Storage, Key Vault, Foundry, and Content Safety |

Azure Verified Modules create the shared services. Small local modules define
the two Container Apps because the GPU resource, volume mount, and probe
contracts need to remain visible and reviewable together.

## Network Flow

The managed environment is injected into a dedicated `/23` infrastructure
subnet. The frontend remains the only public application endpoint and requires
Easy Auth. Inference ingress is `external: false`; its FQDN resolves and routes
only inside the Container Apps environment.

A separate `/24` subnet contains private endpoints. Private DNS zones linked to
the VNet resolve ACR, Azure Files, Key Vault, Foundry, and Content Safety to
private IP addresses. Premium Azure Files uses NFS because the tenant disables
storage shared keys and ACA SMB environment mounts require one. The storage
public endpoint is disabled. ACR retains public control-plane access for ACR
Tasks while managed-identity image pulls use Private Link.

## Request Flow

1. The frontend validates the message and attachments.
2. Content Safety checks text, images, and prompt attacks. Raw audio bypasses
   content moderation by explicit design.
3. Requests involving changing information or explicit web verification call
   Foundry Web Search once with low reasoning and recent conversation context.
4. Prompt Shields checks retrieved evidence for indirect injection. Source URLs
   are included when available but are not required to continue.
5. Apertus receives the evidence, recent turns, media, and profile settings on the one vLLM
   endpoint.
6. Tools-mode can repeat the safe grounding path only when evidence is insufficient.
7. Generated text passes Content Safety and, when grounded, Groundedness Detection
   before browser output.

## Scale and Startup

The frontend keeps one replica available and scales to three at 20 concurrent
HTTP requests. Inference scales from zero to one at one concurrent request and
uses a 30-minute cooldown. Startup probes allow 15 minutes for image streaming,
model download, and initialization. The model and compile caches survive replica
scale-down on Azure Files.

The default serving context is 32,768 tokens. Apertus supports up to 262,144,
but increasing context consumes KV cache and reduces concurrency on one A100.