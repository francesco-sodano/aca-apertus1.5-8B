<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="src/frontend/public/logo_dark.png">
    <img src="src/frontend/public/logo_light.png" alt="Apertus powered by Microsoft Azure" width="760">
  </picture>
</p>

# What is Apertus?

[Apertus](https://www.swiss-ai.org/apertus), Latin for "open," is Switzerland's
open multilingual large-language-model family from the
[Swiss AI Initiative](https://www.swiss-ai.org/). Its training corpus spans
about 15 trillion tokens and more than 1,000 languages, with roughly 40 percent
non-English data. This includes historically underrepresented languages such as
Swiss German and Romansh. The architecture, weights, training documentation,
and recipes are openly published.

This repository deploys the
[`swiss-ai/Apertus-v1.5-8B`](https://huggingface.co/swiss-ai/Apertus-v1.5-8B)
model. It is intended as a transparent building block for multilingual
assistants, translation, education, research, and other applications where open
weights and documented provenance matter.

# Run Apertus on Azure

This project runs Apertus v1.5 8B on an Azure Container Apps serverless NVIDIA
A100 workload profile. A separate Chainlit Container App provides the web
experience, Microsoft Entra authentication, safety checks, adaptive web
grounding, citations, request progress, and operational telemetry.

The implementation adds the security and operational controls needed for a
near-production-ready MVP. It is still an accelerator, not a production
certification: validate capacity, regional availability, safety thresholds,
data-boundary requirements, load, recovery objectives, and organizational
policies before serving production traffic.

## Prerequisites

- A current [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli)
  and [Azure Developer CLI](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd),
  Git, Docker, and OpenSSL on POSIX systems.
- An Azure subscription where you can create resources, role assignments,
  private endpoints, private DNS zones, and a delegated Container Apps subnet.
- `Consumption-GPU-NC24-A100` quota in Sweden Central for one 8B inference
  replica.
- Global Standard quota for `gpt-4.1-nano` in Sweden Central.
- A Hugging Face account that has accepted the Apertus model terms and a
  read-only token.
- Permission to create and update a single-tenant Microsoft Entra app
  registration used by Container Apps built-in authentication.
- An operations email address for Azure Monitor and budget notifications.
- Acceptance of the Grounding with Bing data-boundary terms. Search data can
  leave Azure compliance and geographic boundaries and is not covered by the
  Microsoft Data Protection Addendum.
- Confirmation that the current Sweden Central serverless GPU fleet supports
  the CUDA and NVIDIA driver requirements of the pinned Swiss AI runtime image.

## Architecture

```mermaid
flowchart TB
    User[Authenticated user]

    subgraph ACA[Azure Container Apps environment - VNet injected]
        Auth[Container Apps built-in authentication<br/>Microsoft Entra ID]

        subgraph FrontendApp[Frontend Container App - Consumption CPU]
            Chainlit[Chainlit web UI<br/>HTTPS and WebSocket]
          Pipeline[Application pipeline<br/>Content checks, Apertus tool selection,<br/>citations and progress]
        end

        subgraph InferenceApp[Inference Container App - Consumption-GPU-NC24-A100]
            VLLM[Swiss AI vLLM runtime<br/>internal ingress only]
            Apertus[Apertus v1.5 8B]
            VLLM --> Apertus
        end
    end

    subgraph Foundry[Microsoft Foundry]
        Project[Foundry project]
        WebSearch[Web Search grounding]
        SearchModel[gpt-4.1-nano<br/>search synthesis]
        Project --> WebSearch --> SearchModel
    end

    subgraph SecurityData[Private Azure services]
        Safety[Azure AI Content Safety]
        Vault[Azure Key Vault]
        Registry[Azure Container Registry Premium]
        Files[Azure Files Premium NFS<br/>model cache]
    end

    subgraph Operations[Operations]
        Insights[Application Insights]
        Logs[Log Analytics]
        Budget[Cost Management budget]
    end

    User -->|Sign in| Auth --> Chainlit --> Pipeline
    Pipeline -->|Internal HTTPS and API key| VLLM
    Pipeline -->|Managed identity and Private Link| Project
    Pipeline -->|Managed identity and Private Link| Safety
    Vault -->|Key Vault references| FrontendApp
    Vault -->|Key Vault references| InferenceApp
    Registry -->|Private managed-identity image pull| FrontendApp
    Registry -->|Private managed-identity image pull| InferenceApp
    Files -->|Private NFS mount| VLLM
    FrontendApp --> Insights
    FrontendApp --> Logs
    InferenceApp --> Logs
    Budget --> ACA
```

Only the frontend and inference workloads run in Azure Container Apps.
Microsoft Foundry owns the grounding project, Web Search integration, and
`gpt-4.1-nano` deployment. GPT-4.1 nano is used within the Web Search tool to
retrieve public sources and synthesize a concise cited evidence packet. Apertus
runs inside the private GPU Container App, selects and uses tools, and writes
the final answer.

## Apertus 1.5 Native Tools

This deployment exercises Apertus 1.5's native function-calling capability in
the `Tools` chat profile. Apertus chooses the registered tool and produces its
arguments. The application remains the execution boundary: it never gives the
model arbitrary code, shell, network, or unregistered function access.

The request flow is:

1. High-confidence local tasks such as writing, translation, identity, and
   stable facts bypass tool selection for lower latency.
2. For other requests, Apertus receives one registry-generated native
   `select_tool` function whose enum contains the available tool names and,
   when safe, `none`.
3. A deterministic, non-streaming selector round chooses at most one tool and
   emits JSON arguments. Final answer generation remains streamed.
4. The broker rejects unknown names and invalid JSON Schema arguments, applies
   the tool timeout and one-call request limit, executes the allowlisted
   handler, and safety-screens its output.
5. Apertus receives the tool result through standard assistant tool-call and
   `role="tool"` messages, then writes the final answer. Web results retain
   citations and pass Prompt Shield and groundedness checks.

The built-in registry contains three tools:

| Tool | Apertus selects it for | Input | Key controls |
| --- | --- | --- | --- |
| `search_web` | Current, changing, planned, upcoming, priced, status, news, or explicitly verified public information | Standalone query, maximum 500 characters | Foundry Web Search, strict safe-result instructions, Prompt Shield, Content Safety, at most 8,000 evidence characters and five citations, retries, 120-second timeout |
| `calculator` | Deterministic arithmetic | Numeric expression, maximum 200 characters | AST-only operators, bounded complexity/exponents/results, no `eval`, 2-second timeout |
| `get_current_time` | Current clock time or calendar date in a requested timezone | IANA timezone such as `Europe/Zurich` | IANA validation, no external network call, 2-second timeout |

Explicit current or future requests cannot choose `none`. A malformed native
selector payload gets one bounded correction attempt; subsequent invalid,
unknown, or extra calls fail closed. The UI displays `Apertus selected <Tool>`
and telemetry records selection, completion, rejection, and citation counts
without prompt or tool-result bodies.

New read-only tools are added with a name, user-facing label, precise
description, JSON Schema, timeout, call limit, and asynchronous handler. The
orchestration pipeline does not need tool-specific branches. See the
[frontend tool registry](src/frontend/apertus_frontend/tools.py) and
[frontend guide](src/frontend/README.md).

Stable explanations and writing requests skip Web Search. If the preview
groundedness detector is inconclusive after a search, the application retries
once and can return the cited, safety-screened Web Search summary rather than
discard a successful retrieval.

The public Foundry Responses `web_search` contract does not expose a SafeSearch
request field. This application therefore applies strict safe-result
instructions before retrieval synthesis, then enforces Prompt Shield and
Content Safety over the bounded result before Apertus can use it.

## Azure Services

| Azure service | Documentation | Why it is included |
| --- | --- | --- |
| Azure Container Apps | [Overview](https://learn.microsoft.com/azure/container-apps/overview) and [serverless GPUs](https://learn.microsoft.com/azure/container-apps/gpu-serverless-overview) | Runs the CPU frontend and the isolated A100-backed Apertus inference service with managed scaling and revisions. |
| Microsoft Foundry | [What is Microsoft Foundry?](https://learn.microsoft.com/azure/ai-foundry/what-is-ai-foundry) and [Web Search](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/web-search) | Hosts GPT-4.1 nano and Web Search to retrieve public sources and synthesize the cited evidence packet used by Apertus. |
| Azure AI Content Safety | [Overview](https://learn.microsoft.com/azure/ai-services/content-safety/overview) | Screens user text, images, retrieved evidence, and generated output; detects prompt attacks and reports blocked categories. |
| Azure Container Registry Premium | [Overview](https://learn.microsoft.com/azure/container-registry/container-registry-intro) and [artifact streaming](https://learn.microsoft.com/azure/container-registry/container-registry-artifact-streaming) | Stores private frontend and inference images and accelerates the large inference-image startup path. |
| Azure Files Premium NFS | [NFS file shares](https://learn.microsoft.com/azure/storage/files/files-nfs-protocol) | Persists model and compilation caches without requiring Storage shared-key authentication in the application. |
| Azure Key Vault | [Overview](https://learn.microsoft.com/azure/key-vault/general/overview) | Stores the Hugging Face token, internal vLLM API key, health token, and Entra client secret behind RBAC and Private Link. |
| Azure Virtual Network and Private Link | [Container Apps networking](https://learn.microsoft.com/azure/container-apps/networking) and [Private Link](https://learn.microsoft.com/azure/private-link/private-link-overview) | Isolates runtime traffic and privately connects ACR, Azure Files, Key Vault, Foundry, and Content Safety. |
| Microsoft Entra ID | [Container Apps authentication](https://learn.microsoft.com/azure/container-apps/authentication) | Requires tenant authentication before users can reach the Chainlit application. |
| Managed identities for Azure resources | [Overview](https://learn.microsoft.com/entra/identity/managed-identities-azure-resources/overview) | Removes SDK credentials from containers and grants narrowly scoped access to Foundry, Content Safety, ACR, and Key Vault. |
| Azure Monitor | [Application Insights](https://learn.microsoft.com/azure/azure-monitor/app/app-insights-overview) and [Log Analytics](https://learn.microsoft.com/azure/azure-monitor/logs/log-analytics-overview) | Captures dependency latency, failures, routing decisions, Container Apps logs, and operational alerts without logging prompt bodies. |
| Azure Cost Management | [Budgets](https://learn.microsoft.com/azure/cost-management-billing/costs/tutorial-acm-create-budgets) | Adds resource-group budget notifications for an intentionally expensive GPU workload. |

## Security Measures

The design follows the
[Microsoft Azure Well-Architected security principles](https://learn.microsoft.com/azure/well-architected/security/principles)
and applies least privilege, defense in depth, and private connectivity.

| Security feature | Implementation |
| --- | --- |
| VNet isolation | The Container Apps environment is VNet injected. Apertus inference has internal-only ingress and is reachable only from the frontend over internal HTTPS with an API key. |
| Private endpoints | ACR, Azure Files, Key Vault, Foundry, and Content Safety use approved private endpoints and private DNS zones. |
| User authentication | Container Apps built-in authentication redirects unauthenticated users to a single-tenant Microsoft Entra application. HTTPS is required. |
| Managed identity | The frontend system identity calls Foundry and Content Safety. Separate user-assigned identities receive only `AcrPull` and `Key Vault Secrets User` for platform image pulls and secret references. No `AZURE_CLIENT_ID` selector is injected into the application container. |
| No Storage shared keys | Storage shared-key authorization and public network access are disabled. The model cache uses private Premium NFS rather than an application-held account key. |
| Key Vault protection | Key Vault uses RBAC, purge protection, private access, versionless secret references, and no application secrets in images or committed parameters. |
| Hardened registry | ACR admin, anonymous access, exports, and public networking are disabled at rest. Deployment opens a short authenticated build window and closes it before verification. Runtime pulls use managed identity over Private Link. |
| Safety boundary | Input, image, evidence, and output checks run before content reaches the browser. Prompt Shield protects retrieved evidence from indirect prompt injection. Unsafe content remains a hard block. |
| Tool and grounding boundary | Apertus selects among allowlisted schemas. The broker validates arguments, limits execution, safety-screens outputs, preserves citations, and rejects unknown tools or uncited fallback evidence. |
| Secret handling | Sensitive bootstrap values enter as secure ARM parameters, are stored in Key Vault, and are cleared from the local azd environment after initialization. |
| Observability privacy | Structured telemetry records stage, duration, result, category, application correlation IDs, and sanitized Foundry/APIM support request IDs, but not raw prompts, attachments, evidence, answers, tokens, or secrets. |

Raw audio is the explicit safety exception: MIME type and size are validated,
but Azure AI Content Safety does not inspect its spoken content in this design.
Do not enable audio where policy requires audio moderation.

## Install Apertus v1.5 8B on Azure

This repository deploys one configuration: `swiss-ai/Apertus-v1.5-8B` on an
Azure Container Apps serverless A100 profile in Sweden Central. The commands
below use PowerShell 7. Equivalent Bash hooks are included for Linux and macOS.

### 1. Install the local tools

Install and sign in with current versions of:

- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli), including
  support for `az acr artifact-streaming` and Container Apps commands.
- [Azure Developer CLI](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd).
- Git, Docker, and PowerShell 7. Linux and macOS also require OpenSSL.

Verify the commands before continuing:

```powershell
az version
azd version
git --version
docker version
az acr artifact-streaming --help
```

### 2. Confirm access and capacity

Before deployment, confirm all of the following:

- You can create resources and role assignments in the target subscription.
  `Owner`, or `Contributor` plus `User Access Administrator`, is sufficient.
- Your tenant permits you to create a single-tenant Entra app registration.
- Sweden Central reports `Consumption-GPU-NC24-A100`, and the subscription has
  capacity for one replica.
- The subscription has at least 10 Global Standard units for `gpt-4.1-nano` in
  Sweden Central.
- You accepted the Apertus terms on Hugging Face and created a read-only token.
- You reviewed the Grounding with Bing data-boundary terms described in
  [Prerequisites](#prerequisites).

Check the serverless GPU profile:

```powershell
az containerapp env workload-profile list-supported `
  --location swedencentral `
  --query "[?name=='Consumption-GPU-NC24-A100']" `
  --output table
```

### 3. Clone the repository

```powershell
git clone https://github.com/francesco-sodano/aca-apertus1.5-8B.git
Set-Location aca-apertus1.5-8B
```

### 4. Choose the deployment values

Replace each angle-bracket value. The environment name must contain 5-24
lowercase letters, numbers, or hyphens and must start and end with a letter or
number.

```powershell
$environmentName = '<environment-name>'
$subscriptionId = '<subscription-id>'
$resourceGroup = '<resource-group-name>'
$operationsEmail = '<operations-email>'
$monthlyBudget = '500'
$huggingFaceToken = '<read-only-hugging-face-token>'
```

Use a new, dedicated resource group for this deployment. Do not target a group
that contains unrelated resources; teardown is designed to remove the complete
installation.

Resolve and review the current model commit. Pinning the commit makes later
deployments reproducible:

```powershell
$modelRevision = (Invoke-RestMethod `
  'https://huggingface.co/api/models/swiss-ai/Apertus-v1.5-8B').sha
$modelRevision
```

### 5. Sign in to Azure

```powershell
az login
az account set --subscription $subscriptionId
azd auth login

$tenantId = az account show --query tenantId --output tsv
```

Confirm that `az account show` displays the intended tenant and subscription
before continuing.

### 6. Create the frontend Entra application

This is required once for each installation. The deployment hook adds the final
Container Apps callback URL after Azure creates the frontend endpoint.

```powershell
$clientId = az ad app create `
  --display-name "apertus-$environmentName" `
  --sign-in-audience AzureADMyOrg `
  --query appId `
  --output tsv

az ad sp create --id $clientId --output none

$clientSecret = az ad app credential reset `
  --id $clientId `
  --display-name 'apertus-container-app' `
  --query password `
  --output tsv
```

Keep `$clientSecret` in the current terminal. Do not write it to a file or
commit it.

### 7. Create the azd environment

```powershell
azd env new $environmentName
azd env set AZURE_SUBSCRIPTION_ID $subscriptionId
azd env set AZURE_RESOURCE_GROUP $resourceGroup
azd env set AZURE_LOCATION swedencentral
azd env set APERTUS_MODEL_REVISION $modelRevision
azd env set ACCEPT_APERTUS_LICENSE true
azd env set ACCEPT_BING_GROUNDING_TERMS true
azd env set HF_TOKEN $huggingFaceToken
azd env set ENTRA_TENANT_ID $tenantId
azd env set ENTRA_CLIENT_ID $clientId
azd env set ENTRA_CLIENT_SECRET $clientSecret
azd env set ALERT_EMAIL $operationsEmail
azd env set MONTHLY_BUDGET_AMOUNT $monthlyBudget
```

The deployment derives globally unique ACR, Storage, Key Vault, Foundry, and
Content Safety names from the subscription, resource group, and azd environment.
Do not set resource names unless your organization requires specific globally
unique names. Optional overrides are:

- `AZURE_CONTAINER_REGISTRY_NAME`
- `AZURE_STORAGE_ACCOUNT_NAME`
- `AZURE_KEY_VAULT_NAME`
- `AZURE_FOUNDRY_ACCOUNT_NAME`
- `AZURE_CONTENT_SAFETY_ACCOUNT_NAME`

Frontend images use public PyPI by default. Organizations can set the generic
build-time `PYTHON_PACKAGE_INDEX_URL` azd value to another PEP 503-compatible
index without changing the Dockerfile or publishing an internal registry URL.

azd supplies `AZURE_ENV_NAME` and `AZURE_PRINCIPAL_ID`. The preprovision hook
generates the internal vLLM API key and model-health token. Do not create or set
those values manually.

### 8. Preview the infrastructure

This step authenticates and evaluates the Bicep deployment without applying it.
It does not run the deployment hooks or create internal secrets:

```powershell
azd provision --preview --no-prompt
```

Review the target subscription, resource group, location, generated names, and
resource changes. Resolve policy, quota, permission, or naming errors before
deployment.

### 9. Deploy everything

```powershell
azd up
```

`azd up` is the single deployment command. It:

1. Runs preflight validation and generates internal secrets.
2. Provisions the VNet, private endpoints, identities, RBAC, Container Apps,
   Foundry, Content Safety, Key Vault, storage, ACR, monitoring, and budget.
3. Stores bootstrap secrets in Key Vault and clears their local azd values.
4. Opens a temporary authenticated ACR build window, builds the immutable
   inference image, and requires successful artifact-stream conversion before
   promoting it.
5. Configures Entra authentication and deploys the frontend image.
6. Restores ACR to private, default-deny, export-disabled operation, enables
   authenticated ingress, and attempts to prewarm Apertus.

The inference app is intentionally not a normal azd service. The postprovision
hook promotes it only after artifact-stream conversion succeeds. The first run
can take 45-90 minutes because it creates private infrastructure, builds and
converts the large inference image, and loads the model. Repeating `azd up`
without changes to the committed `src/inference` tree reuses the immutable
inference image.

### 10. Verify the installation

```powershell
$frontendUri = azd env get-value SERVICE_FRONTEND_URI
$registryName = azd env get-value AZURE_CONTAINER_REGISTRY_NAME

Invoke-RestMethod "$frontendUri/healthz"
Invoke-RestMethod "$frontendUri/healthz/ready"

az acr show `
  --name $registryName `
  --query "{publicNetworkAccess:publicNetworkAccess,defaultAction:networkRuleSet.defaultAction,exportPolicy:policies.exportPolicy.status,adminUserEnabled:adminUserEnabled}" `
  --output table

Start-Process $frontendUri
```

Both health endpoints must return a JSON status. ACR must report public network
access disabled, default action `Deny`, exports disabled, and admin disabled.
The browser must redirect to Microsoft Entra sign-in. After signing in, send one
prompt to verify that Apertus inference is warm and responding.

### Update the deployment

Commit inference changes before deploying so the inference-tree-derived
immutable image tag changes. Then run the same deployment command:

```powershell
git pull
azd up
```

For a deliberate inference rebuild without a new Git commit, set a unique tag
for that run, deploy, and clear the override:

```powershell
azd env set APERTUS_IMAGE_TAG_OVERRIDE '<unique-tag>'
azd up
azd env set APERTUS_IMAGE_TAG_OVERRIDE ''
```

### Rotate application secrets

Create a fresh Hugging Face token and append a new Entra credential, then run:

```powershell
$clientId = azd env get-value ENTRA_CLIENT_ID
$newClientSecret = az ad app credential reset `
  --id $clientId `
  --append `
  --display-name 'apertus-container-app-rotation' `
  --query password `
  --output tsv

azd env set HF_TOKEN '<new-read-only-hugging-face-token>'
azd env set ENTRA_CLIENT_SECRET $newClientSecret
azd env set ROTATE_APPLICATION_SECRETS true
azd up
```

After a successful rotation, the hook clears the source secrets and resets
`ROTATE_APPLICATION_SECRETS=false`. Remove the old Entra credential only after
the application is verified.

The internal vLLM key rotates independently without replacing the Hugging Face
or Entra credentials:

```powershell
azd env set ROTATE_VLLM_SECRET true
azd up
```

Preprovision generates the replacement, Key Vault stores it, both Container
Apps receive versionless secret references, and a successful deployment resets
`ROTATE_VLLM_SECRET=false`.

### Recover from an interrupted deployment

Postprovision failures close the temporary ACR build window automatically. If
the process is interrupted or frontend deployment fails after the window opens,
restore ACR explicitly before retrying:

```powershell
$registryName = azd env get-value AZURE_CONTAINER_REGISTRY_NAME
az acr update `
  --name $registryName `
  --allow-exports false `
  --public-network-enabled false `
  --default-action Deny `
  --output none

azd up
```

Provisioning, image build, conversion, and promotion are resumable. A failed
artifact conversion never updates the inference app. The budget start date is
captured from the first successful deployment and passed back through
`BUDGET_START_DATE`, avoiding immutable-date failures on later deployments.

### Remove the installation

Confirm the selected azd environment, subscription, and resource group, then
remove the deployment:

```powershell
azd env select $environmentName
$clientId = azd env get-value ENTRA_CLIENT_ID
azd down --purge
az ad app delete --id $clientId
azd env remove $environmentName --force
```

`--purge` permanently deletes soft-deleted Key Vault data. Use it only when the
entire installation must be removed. The Entra application is created outside
Bicep, so it must be deleted separately after Azure resources are removed.

## Validate Locally

```powershell
Set-Location src/frontend
uv sync --dev
uv run pytest
uv run python -m compileall app.py apertus_frontend
Set-Location ../..

az bicep build --file infra/main.bicep
bash -n infra/hooks/*.sh src/inference/docker-entrypoint.sh
bash infra/hooks/tests/postprovision-test.sh

docker build --check --file src/frontend/Dockerfile src/frontend
docker build --check --file src/inference/Dockerfile src/inference
```

Running the frontend locally requires Content Safety, Foundry, and an
OpenAI-compatible Apertus endpoint. See
[the frontend guide](src/frontend/README.md).

## Operations

- [Inference runtime](src/inference/README.md)

The deployment includes an email action group, a monthly resource-group budget,
frontend timeout alerts, and inference restart alerts. Admission control limits
each authenticated principal to six requests per minute and four concurrent
requests per frontend replica.

## License

This project is licensed under the [MIT License](LICENSE). Assets copied from the
Azure Samples Swiss LLM quickstart retain their original
[MIT license and attribution](src/frontend/public/LICENSE.azure-samples.md).

## Citation

If this project is useful in research, a publication, a demo, or another
implementation, please cite the Apertus model and acknowledge this Azure
implementation and its project team.

```bibtex
@misc{swissai2025apertus,
  title={{Apertus: Democratizing Open and Compliant LLMs for Global Language Environments}},
  author={Apertus Team},
  year={2025},
  howpublished={\url{https://huggingface.co/swiss-ai/Apertus-v1.5-8B}}
}

@software{sodano2026apertusazure,
  title={Apertus 1.5 8B on Azure Container Apps},
  author={Francesco Sodano and Dominique Broeglin},
  year={2026},
  url={https://github.com/francesco-sodano/aca-apertus1.5-8B}
}
```

For reproducibility, also record the exact `swiss-ai/Apertus-v1.5-8B` commit
configured through `APERTUS_MODEL_REVISION`.

## Content Owners and Attribution

- [Francesco Sodano](https://github.com/francesco-sodano)
- [Dominique Broeglin](https://github.com/dbroeglin)

The visual assets under `src/frontend/public` originate from the
[Azure Samples Swiss LLM quickstart](https://github.com/Azure-Samples/swiss-llm-quickstart/tree/feature/frontend/src/frontend/public).
