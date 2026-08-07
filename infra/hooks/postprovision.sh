#!/usr/bin/env bash
set -euo pipefail

: "${AZURE_CONTAINER_REGISTRY_NAME:?Missing azd output AZURE_CONTAINER_REGISTRY_NAME}"
: "${AZURE_CONTAINER_REGISTRY_ENDPOINT:?Missing azd output AZURE_CONTAINER_REGISTRY_ENDPOINT}"
: "${AZURE_KEY_VAULT_NAME:?Missing azd output AZURE_KEY_VAULT_NAME}"
: "${AZURE_RESOURCE_GROUP:?Missing AZURE_RESOURCE_GROUP}"
: "${FRONTEND_IDENTITY_PRINCIPAL_ID:?Missing frontend identity principal ID}"
: "${INFERENCE_IDENTITY_PRINCIPAL_ID:?Missing inference identity principal ID}"
: "${SERVICE_FRONTEND_NAME:?Missing azd output SERVICE_FRONTEND_NAME}"
: "${SERVICE_FRONTEND_URI:?Missing azd output SERVICE_FRONTEND_URI}"
: "${SERVICE_INFERENCE_NAME:?Missing azd output SERVICE_INFERENCE_NAME}"
: "${AZURE_ENV_NAME:?Missing AZURE_ENV_NAME}"
: "${ENTRA_CLIENT_ID:?ENTRA_CLIENT_ID is required}"
: "${ENTRA_TENANT_ID:?ENTRA_TENANT_ID is required}"

inference_source_tag="$(git rev-parse --short=12 HEAD:src/inference 2>/dev/null || echo local)"
image_tag="${APERTUS_IMAGE_TAG_OVERRIDE:-1.5.0-${AZURE_ENV_NAME}-${inference_source_tag}}"
inference_repository='apertus/inference'
acr_build_window_open='false'

cleanup_failed_postprovision() {
  local status="$?"
  trap - EXIT
  if [[ "${status}" -ne 0 && "${acr_build_window_open}" == 'true' ]]; then
    echo 'Postprovision failed; restoring the private ACR posture.' >&2
    if ! az acr update \
      --name "${AZURE_CONTAINER_REGISTRY_NAME}" \
      --allow-exports false \
      --public-network-enabled false \
      --default-action Deny \
      --output none; then
      echo 'CRITICAL: failed to restore the private ACR posture.' >&2
    fi
  fi
  exit "${status}"
}
trap cleanup_failed_postprovision EXIT

# Bootstrap secrets are now in Key Vault; remove source copies from local azd state.
azd env set APPLICATION_SECRETS_READY true >/dev/null
azd env set HF_TOKEN '' >/dev/null
azd env set ENTRA_CLIENT_SECRET '' >/dev/null
azd env set VLLM_API_KEY '' >/dev/null

acr_resource_id="$(az acr show \
  --name "${AZURE_CONTAINER_REGISTRY_NAME}" \
  --query id \
  --output tsv)"
if [[ -z "${acr_resource_id}" ]]; then
  echo 'Could not resolve the ACR resource ID for the RBAC gate.' >&2
  exit 1
fi

# Do not push private images until both runtime identities can pull from ACR.
for identity in \
  "frontend:${FRONTEND_IDENTITY_PRINCIPAL_ID}" \
  "inference:${INFERENCE_IDENTITY_PRINCIPAL_ID}"; do
  identity_name="${identity%%:*}"
  principal_id="${identity#*:}"
  role=''
  for attempt in $(seq 1 10); do
    role="$(az role assignment list \
      --scope "${acr_resource_id}" \
      --assignee-object-id "${principal_id}" \
      --query "[?roleDefinitionName=='AcrPull'].roleDefinitionName" \
      --output tsv 2>/dev/null || true)"
    [[ "${role}" == 'AcrPull' ]] && break
    [[ "${attempt}" == '10' ]] || sleep 30
  done
  if [[ "${role}" != 'AcrPull' ]]; then
    printf 'AcrPull did not propagate for the %s identity.\n' "${identity_name}" >&2
    exit 1
  fi
  printf 'AcrPull confirmed for the %s identity.\n' "${identity_name}"
done

# ACR Tasks is Microsoft hosted, so the private registry opens only for the build.
echo 'Opening the authenticated ACR build window.'
az acr update \
  --name "${AZURE_CONTAINER_REGISTRY_NAME}" \
  --allow-exports true \
  --public-network-enabled true \
  --default-action Allow \
  --output none
acr_build_window_open='true'

acr_data_plane_ready='false'
for attempt in $(seq 1 6); do
  if az acr repository list \
    --name "${AZURE_CONTAINER_REGISTRY_NAME}" \
    --top 1 \
    --output none 2>/dev/null; then
    acr_data_plane_ready='true'
    break
  fi
  if [[ "${attempt}" != '6' ]]; then
    printf 'ACR data plane is still propagating (%s/6); retrying.\n' "${attempt}"
    sleep 30
  fi
done
if [[ "${acr_data_plane_ready}" != 'true' ]]; then
  echo 'ACR data plane did not become reachable.' >&2
  exit 1
fi

echo "Building the immutable inference image in ${AZURE_CONTAINER_REGISTRY_NAME}..."
image_digest="$(az acr repository show \
  --name "${AZURE_CONTAINER_REGISTRY_NAME}" \
  --image "${inference_repository}:${image_tag}" \
  --query digest \
  --output tsv 2>/dev/null || true)"
if [[ -n "${image_digest}" ]]; then
  printf 'Reusing existing immutable inference image %s:%s.\n' "${inference_repository}" "${image_tag}"
else
  build_succeeded='false'
  for attempt in $(seq 1 6); do
    if az acr build \
      --registry "${AZURE_CONTAINER_REGISTRY_NAME}" \
      --image "${inference_repository}:${image_tag}" \
      src/inference; then
      build_succeeded='true'
      break
    fi
    if [[ "${attempt}" != '6' ]]; then
      printf 'ACR build plane is still propagating (%s/6); retrying.\n' "${attempt}"
      sleep 30
    fi
  done
  if [[ "${build_succeeded}" != 'true' ]]; then
    echo 'Inference image build failed.' >&2
    exit 1
  fi
fi

# The GPU app is promoted only after the exact immutable tag is streamable.
streaming_status="$(az acr artifact-streaming operation show \
  --name "${AZURE_CONTAINER_REGISTRY_NAME}" \
  --image "${inference_repository}:${image_tag}" \
  --query 'status' \
  --output tsv 2>/dev/null || true)"
if [[ "${streaming_status}" == 'Succeeded' ]]; then
  echo 'Reusing successful artifact-stream conversion.'
else
  echo "Enabling artifact-stream auto-conversion for ${inference_repository}..."
  az acr artifact-streaming update \
    --name "${AZURE_CONTAINER_REGISTRY_NAME}" \
    --repository "${inference_repository}" \
    --enable-streaming true \
    --output none

  echo "Creating streaming artifact for ${inference_repository}:${image_tag}..."
  az acr artifact-streaming create \
    --name "${AZURE_CONTAINER_REGISTRY_NAME}" \
    --image "${inference_repository}:${image_tag}" \
    --output none

  streaming_status='Unknown'
  for _ in $(seq 1 90); do
    streaming_status="$(az acr artifact-streaming operation show \
      --name "${AZURE_CONTAINER_REGISTRY_NAME}" \
      --image "${inference_repository}:${image_tag}" \
      --query 'status' \
      --output tsv 2>/dev/null || echo Unknown)"
    printf 'Artifact streaming status: %s\n' "${streaming_status}"
    [[ "${streaming_status}" == 'Succeeded' ]] && break
    if [[ "${streaming_status}" == 'Failed' || "${streaming_status}" == 'Canceled' ]]; then
      echo 'Artifact-stream conversion failed; inference app will not be updated.' >&2
      exit 1
    fi
    sleep 20
  done
fi

if [[ "${streaming_status}" != 'Succeeded' ]]; then
  echo 'Artifact-stream conversion timed out; inference app will not be updated.' >&2
  exit 1
fi

# Container Apps resolves these versionless references through managed identity.
key_vault_uri="https://${AZURE_KEY_VAULT_NAME}.vault.azure.net/secrets"
az containerapp secret set \
  --name "${SERVICE_INFERENCE_NAME}" \
  --resource-group "${AZURE_RESOURCE_GROUP}" \
  --secrets \
    "hugging-face-token=keyvaultref:${key_vault_uri}/hugging-face-token,identityref:${INFERENCE_IDENTITY_RESOURCE_ID:-system}" \
    "vllm-api-key=keyvaultref:${key_vault_uri}/vllm-api-key,identityref:${INFERENCE_IDENTITY_RESOURCE_ID:-system}" \
  --output none

az containerapp secret set \
  --name "${SERVICE_FRONTEND_NAME}" \
  --resource-group "${AZURE_RESOURCE_GROUP}" \
  --secrets \
    "vllm-api-key=keyvaultref:${key_vault_uri}/vllm-api-key,identityref:${FRONTEND_IDENTITY_RESOURCE_ID:-system}" \
    "model-health-token=keyvaultref:${key_vault_uri}/model-health-token,identityref:${FRONTEND_IDENTITY_RESOURCE_ID:-system}" \
    "entra-client-secret=keyvaultref:${key_vault_uri}/entra-client-secret,identityref:${FRONTEND_IDENTITY_RESOURCE_ID:-system}" \
  --output none

# Preserve existing redirects while registering the generated Container Apps callback.
callback_uri="${SERVICE_FRONTEND_URI}/.auth/login/aad/callback"
redirect_uris=("${callback_uri}")
while IFS= read -r existing_redirect_uri; do
  if [[ -n "${existing_redirect_uri}" && "${existing_redirect_uri}" != "${callback_uri}" ]]; then
    redirect_uris+=("${existing_redirect_uri}")
  fi
done < <(az ad app show --id "${ENTRA_CLIENT_ID}" --query web.redirectUris --output tsv)

az ad app update \
  --id "${ENTRA_CLIENT_ID}" \
  --enable-id-token-issuance true \
  --web-redirect-uris "${redirect_uris[@]}" \
  --output none

az containerapp auth microsoft update \
  --name "${SERVICE_FRONTEND_NAME}" \
  --resource-group "${AZURE_RESOURCE_GROUP}" \
  --client-id "${ENTRA_CLIENT_ID}" \
  --client-secret-name entra-client-secret \
  --tenant-id "${ENTRA_TENANT_ID}" \
  --allowed-audiences "${ENTRA_CLIENT_ID}" \
  --yes \
  --output none

az containerapp auth update \
  --name "${SERVICE_FRONTEND_NAME}" \
  --resource-group "${AZURE_RESOURCE_GROUP}" \
  --enabled true \
  --unauthenticated-client-action RedirectToLoginPage \
  --redirect-provider AzureActiveDirectory \
  --require-https true \
  --token-store false \
  --yes \
  --output none

# These updates create revisions only after artifacts, secrets, and auth are ready.
echo 'Artifact conversion succeeded; promoting inference image.'
az containerapp update \
  --name "${SERVICE_INFERENCE_NAME}" \
  --resource-group "${AZURE_RESOURCE_GROUP}" \
  --container-name inference \
  --image "${AZURE_CONTAINER_REGISTRY_ENDPOINT}/${inference_repository}:${image_tag}" \
  --set-env-vars 'HF_TOKEN=secretref:hugging-face-token' 'VLLM_API_KEY=secretref:vllm-api-key' \
  --output none

az containerapp update \
  --name "${SERVICE_FRONTEND_NAME}" \
  --resource-group "${AZURE_RESOURCE_GROUP}" \
  --container-name frontend \
  --set-env-vars 'VLLM_API_KEY=secretref:vllm-api-key' 'MODEL_HEALTH_TOKEN=secretref:model-health-token' \
  --output none

azd env set APERTUS_IMAGE_TAG '' >/dev/null
azd env set ROTATE_APPLICATION_SECRETS false >/dev/null
azd env set ROTATE_VLLM_SECRET false >/dev/null
echo 'Inference image promoted and frontend secrets configured successfully.'
