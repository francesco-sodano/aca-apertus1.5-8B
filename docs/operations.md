# Operations

## Deployment Gates

Before `azd up`, verify:

1. `Consumption-GPU-NC24-A100` is listed in Sweden Central and the subscription
   has at least one available GPU replica.
2. The selected Foundry deployment has Global Standard `gpt-4.1-nano` quota.
3. The Apertus model terms are accepted and its exact Hugging Face commit is
   pinned in `APERTUS_MODEL_REVISION`.
4. The active Container Apps GPU driver supports the digest-pinned CUDA 13
   runtime.
5. Grounding with Bing terms and ACR artifact-streaming preview terms are
   acceptable for the workload.
6. The Entra app registration is single tenant and the deploying principal can
   update its web redirect URIs.
7. `ALERT_EMAIL` and `MONTHLY_BUDGET_AMOUNT` match the operating team's policy.
8. MCAPSGov policy still requires disabled Storage shared keys/public access,
   disabled Key Vault public access, and disabled Cognitive Services local auth.

The preprovision hook checks location, profile availability, required CLI
commands, required values, and explicit terms flags. Quota and GPU driver fleet
state still require subscription and regional confirmation.

Live validation on 2026-07-29 UTC created a new workload-profile environment in
Sweden Central under the `cloudcherry-prod` subscription. Azure assigned an
environment-scoped A100 quota of 2, and a running replica consumed 1 of 2. The
replica reported an NVIDIA A100 80GB PCIe, driver `580.105.08`, CUDA
compatibility `13.0`, and container CUDA runtime `13.0.2`. No Container Apps GPU
quota support ticket is currently required for this subscription. Recheck the
target environment because GPU quota is environment scoped and fleet versions
can change.

The final `apertus-dev` deployment completed on 2026-07-30 in
`rg-cloudcherry-apertus`. The public authenticated endpoint is
`https://ca-apertus-dev-frontend.greenfield-03ce481d.swedencentral.azurecontainerapps.io/`.
Both active revisions were healthy, model prewarm succeeded, and a private
generation smoke test returned `APERTUS_OK`.

## Image Promotion

Postprovision builds immutable, Git-derived tags in ACR. The inference sequence
is deliberately transactional:

1. Build the inference tag remotely in ACR.
2. Enable automatic streaming conversion on `apertus/inference`.
3. Create a streaming artifact for the exact inference tag.
4. Poll until status is `Succeeded`.
5. Configure Key Vault references.
6. Update the inference image and configure frontend secret references.
7. Let the normal azd deploy stage build and deploy the frontend service.
8. Enable authenticated external ingress after the real frontend image is active.

The registry is private and export-disabled at rest. Postprovision opens an
authenticated public build window before step 1 so Microsoft-hosted ACR Tasks
and azd can push images. Postdeploy closes that window before step 8. If a deploy
fails between those hooks, rerun `azd deploy --no-prompt` or manually set ACR
public network access to disabled and its default action to deny.
The hook retries the inference build while the ACR firewall change propagates to
the data plane; a bounded retry failure remains fatal. If the immutable tag was
already pushed by a prior attempt, rerunning provision reuses it and resumes at
artifact-stream conversion. A successful conversion is also reused on a later
resume, proceeding directly to authentication and image promotion.

`Failed`, `Canceled`, unknown timeout, build errors, or secret errors stop the
hook. The previous Container App revision remains active. Never update inference
by digest: artifact streaming currently operates on compatible Linux AMD64 tags.

## Health and Prewarm

| Route | Use |
| --- | --- |
| `/healthz` | Frontend startup and liveness |
| `/healthz/ready` | Frontend initialization readiness without waking the GPU |
| `/healthz/model` | Authenticated operator prewarm and inference dependency check |
| vLLM `/health` | Internal inference startup, readiness, and liveness |

Postdeploy calls `/healthz/model` with the locally retained one-time deployment
copy of the Key Vault-backed bearer token for up to 20 minutes, then clears that
local value. The vault remains private throughout. Timeout is nonfatal because
scale-to-zero remains valid; the first user request then pays the cold-start cost.

## Authentication and Admission

Container Apps Easy Auth redirects browser users to Microsoft Entra ID. Health
routes are excluded from Easy Auth so platform probes work; `/healthz/model`
still requires its separate operator bearer token in application code.

The frontend allows four in-flight requests per replica and six requests per
authenticated principal per minute. A request waits up to five seconds for
capacity, then receives a retry-later response. Container Apps scales frontend
replicas at four concurrent HTTP requests. The single GPU replica remains the
ultimate throughput boundary.

## Secret Rotation

Normal deployments do not replace Key Vault values. To rotate all application
secrets, set `ROTATE_APPLICATION_SECRETS=true`. For Entra, first create a fresh
credential and set `ENTRA_CLIENT_SECRET`; also set the current `HF_TOKEN`.
Preprovision generates new internal tokens and ARM writes every new secret
version without public vault access. Run `azd up`, verify both apps, remove the
old Entra credential, and reset `ROTATE_APPLICATION_SECRETS=false`.

## Logs

Container console and system logs are stored in Log Analytics for 30 days.
Frontend OpenTelemetry flows to Application Insights. Example queries:

```kusto
ContainerAppConsoleLogs_CL
| where TimeGenerated > ago(1h)
| project TimeGenerated, ContainerAppName_s, Log_s
| order by TimeGenerated desc
```

```kusto
ContainerAppSystemLogs_CL
| where TimeGenerated > ago(1h)
| where Reason_s has_any ('Failed', 'Unhealthy', 'PullingImage')
| order by TimeGenerated desc
```

Use correlation IDs returned by frontend errors to join application exceptions
and dependency traces. Do not enable body capture or add prompt text to logs.

The deployment creates an action group for `ALERT_EMAIL`, request-timeout and
inference-restart metric alerts, and a resource-group budget. Budget notices fire
at 70, 90, and 100 percent of `MONTHLY_BUDGET_AMOUNT`.

## Rollback

List known tags and revisions:

```powershell
az acr repository show-tags --name $env:AZURE_CONTAINER_REGISTRY_NAME --repository apertus/inference --orderby time_desc
az containerapp revision list --name $env:SERVICE_INFERENCE_NAME --resource-group $env:AZURE_RESOURCE_GROUP -o table
```

Verify the target tag has a streaming referrer before rollback, then update the
Container App by tag. In single-revision mode this creates and activates a new
revision. Do not promote an unverified tag.

## Cost

The largest variable cost is the A100 replica while active. Scale-to-zero removes
idle GPU compute charges, but cold starts remain. Persistent charges include
Premium ACR, Premium NFS Azure Files, private endpoints, Log Analytics ingestion/retention, Application
Insights, Key Vault operations, Content Safety calls, Foundry model tokens, and
Web Search tool calls.

Use the Azure Pricing Calculator with Sweden Central rates before deployment.
Keep the inference maximum at one unless both quota and budget explicitly permit
more. Reduce Log Analytics verbosity or retention only after confirming audit
and incident requirements.

## Cleanup

```powershell
azd down --purge
```

Confirm that the selected azd environment maps to the intended resource group.
`--purge` permanently removes soft-deleted Key Vault data as part of cleanup.