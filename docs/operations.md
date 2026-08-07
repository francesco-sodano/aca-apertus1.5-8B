# Operations

## Deployment Gates

Before `azd up`, verify:

1. Sweden Central supports `Consumption-GPU-NC24-A100` and the subscription has
   one available GPU replica.
2. GPT-4.1 nano Global Standard capacity is available.
3. The exact Apertus Hugging Face revision is pinned and its terms are accepted.
4. Grounding with Bing terms are accepted for the workload.
5. The Entra application is single tenant and the deployer can update redirects.
6. Alert email and monthly budget match operating policy.
7. Private endpoints, identities, role assignments, and disabled local/shared
   key authentication are permitted by policy.

The preprovision hook validates location, names, required values, terms flags,
the GPU profile, and artifact-streaming CLI support. Capacity and fleet driver
state still require live verification.

## Image Promotion

Inference promotion is transactional:

1. Verify `AcrPull` propagation for both workload identities.
2. Open the authenticated ACR build window.
3. Build or reuse the inference image tagged by azd environment and committed
   `src/inference` tree hash.
4. Enable artifact streaming and require conversion status `Succeeded`.
5. Configure versionless Key Vault references and Entra authentication.
6. Promote the inference image only after conversion succeeds.
7. Deploy the frontend with azd.
8. Restore ACR to private/default-deny/export-disabled and enable authenticated
   frontend ingress.

Failed, canceled, or timed-out conversion never updates inference. Hook failures
restore ACR automatically. If the host process is terminated externally while
the build window is open, restore ACR manually before retrying.

## Health and Readiness

| Route | Purpose |
| --- | --- |
| Frontend `/healthz` | Startup and liveness |
| Frontend `/healthz/ready` | Frontend initialization without waking the GPU |
| Frontend `/healthz/model` | Authenticated operator prewarm and dependency check |
| Inference `/health` | vLLM startup, readiness, and liveness |

Inference can take several minutes to load the model and capture graphs after
scale-up. The first user request can pay this cold-start cost when prewarm is not
available.

## Authentication and Admission

Container Apps Easy Auth redirects unauthenticated users to Entra ID. Public
health routes remain available for platform probes; `/healthz/model` requires a
separate bearer token.

Each frontend replica permits four in-flight requests and six requests per
authenticated principal per minute. Sticky sessions improve consistency, but
these are in-memory **per-replica** limits. Use a shared distributed limiter if
the deployment needs a strict global quota across three frontend replicas.

## Secret Rotation

Normal deployments preserve existing Key Vault versions.

- Full rotation: set fresh `HF_TOKEN` and `ENTRA_CLIENT_SECRET`, then set
  `ROTATE_APPLICATION_SECRETS=true` and run `azd up`.
- vLLM-only rotation: set `ROTATE_VLLM_SECRET=true` and run `azd up`.

Successful hooks clear source values and reset rotation flags. Remove superseded
Entra credentials only after application verification.

## Monitoring

Container logs are stored in Log Analytics. Frontend OpenTelemetry is exported
to workspace-based Application Insights. Use correlation IDs to join errors with
dependency traces. Do not enable request/response body capture.

Foundry search telemetry includes sanitized client, response, and APIM request
IDs, citation count, duration, evidence size, and Web Search action count. It
does not include prompts, search queries, evidence, answers, or credentials.

## Rollback

List revisions and immutable tags, verify the target image exists and has a
successful artifact-streaming referrer, then activate or redeploy the previous
verified tag. Never promote an unverified inference image or leave ACR public.

## Cost

The largest variable cost is the A100 replica while active. Persistent charges
include Premium ACR, Premium NFS Azure Files, private endpoints, telemetry,
Key Vault operations, Content Safety, Foundry model tokens, and Grounding with
Bing search transactions. Search action counts are logged so repeated Bing calls
can be measured before changing the grounding model.

## Cleanup

```powershell
azd down --purge
```

Confirm the selected azd environment and resource group first. `--purge`
permanently removes soft-deleted Key Vault data. Delete the separately created
Entra application only after Azure resource cleanup succeeds.