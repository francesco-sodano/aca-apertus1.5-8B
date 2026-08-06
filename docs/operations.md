# Operations

## Deployment Gates

Before `azd up`, verify:

1. `Consumption-GPU-NC24-A100` is listed in Sweden Central and the subscription
   has at least one available GPU replica.
2. The Microsoft Web IQ profile is active, not expired, and allows Web Search.
3. The Apertus model terms are accepted and its exact Hugging Face commit is
   pinned in `APERTUS_MODEL_REVISION`.
4. The active Container Apps GPU driver supports the digest-pinned CUDA 13
   runtime.
5. Microsoft Web IQ terms and ACR artifact-streaming preview terms are
   acceptable for the workload.
6. The Entra app registration is single tenant and the deploying principal can
   update its web redirect URIs.
7. `ALERT_EMAIL` and `MONTHLY_BUDGET_AMOUNT` match the operating team's policy.
8. The target subscription permits the required private endpoints, managed
   identities, role assignments, and disabled local/shared-key authentication.

The preprovision hook checks location, profile availability, required CLI
commands, required values, and explicit terms flags. Quota and GPU driver fleet
state still require subscription and regional confirmation.

GPU quota is environment scoped and managed fleet versions can change. After
deployment, verify the assigned quota, active hardware, driver, and CUDA
compatibility from the target environment rather than relying on values from a
different subscription or an earlier deployment.

Confirm the generated endpoint and revision health with `azd show` and
`az containerapp revision list`. The application root must require Entra sign-in,
the two public health routes must return `200`, and the inference app must expose
no public FQDN.

## Image Promotion

Postprovision builds immutable tags from the azd environment and committed
`src/inference` tree. `APERTUS_IMAGE_TAG_OVERRIDE` is available only for a
deliberate rebuild without a new inference commit. The inference sequence is
deliberately transactional:

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
and azd can push images. A postprovision failure closes that window before
returning an error; postdeploy closes it again before step 8. If the process is
terminated externally between those hooks, restore ACR to public-network
disabled, default deny, and exports disabled before retrying `azd up`.
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
version without public vault access. Run `azd up`, verify both apps, and remove
the old Entra credential. A successful postprovision automatically resets
`ROTATE_APPLICATION_SECRETS=false`.

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
Premium ACR, Premium NFS Azure Files, private endpoints, Log Analytics
ingestion/retention, Application Insights, Key Vault operations, Content Safety
calls, and Web IQ API calls.

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