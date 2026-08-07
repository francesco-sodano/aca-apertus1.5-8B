# Architecture

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

1. The frontend validates message and attachment limits.
2. User text passes Prompt Shield and Analyze Text. Images pass Analyze Image.
   Raw audio follows the documented moderation exception.
3. High-confidence local tasks bypass tools. Otherwise Apertus receives a
   forced registry-generated selector and chooses one allowlisted tool or
   `none`; explicit current/future requests cannot choose `none`.
4. The broker validates JSON Schema arguments, enforces a timeout and one-call
   limit, executes the selected handler, and moderates its output.
5. Web Search sends one forced, low-context Responses API request to Foundry.
   The returned summary is capped at 8,000 characters and citations at five.
6. Retrieved evidence passes Prompt Shield before Apertus receives it.
7. Apertus generates the final answer from the approved tool result and recent
   conversation context.
8. Generated text passes Analyze Text. Grounded answers also pass Groundedness
   Detection. A failed or indeterminate groundedness decision gets one bounded
   regeneration; when that still fails, only the cited, safety-screened Foundry
   summary can be returned.
9. User-facing blocks name the responsible layer and rule. Severity, threshold,
   prompts, evidence, and model output remain telemetry-only.

## Scale and State

The frontend keeps one replica available and scales to three at four concurrent
HTTP requests. Sticky sessions improve conversation and in-process admission
consistency. The six-requests-per-minute principal limit and four-request
concurrency gate are **per frontend replica**, not global distributed quotas.

Inference scales from zero to one at one concurrent request and uses a 30-minute
cooldown. Startup probes allow 15 minutes for artifact streaming, model loading,
and graph initialization. Model and compilation caches survive scale-down on
Premium Azure Files.

The serving context defaults to 32,768 tokens. Apertus supports up to 262,144,
but increasing context consumes KV cache and reduces concurrency on one A100.