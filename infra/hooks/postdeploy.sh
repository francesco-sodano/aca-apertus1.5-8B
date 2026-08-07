#!/usr/bin/env bash
set -u

: "${AZURE_CONTAINER_REGISTRY_NAME:?Container Registry name is missing; the deployment build window cannot be closed}"

# Always restore the private, export-disabled registry posture before exposure.
if ! az acr update \
  --name "${AZURE_CONTAINER_REGISTRY_NAME}" \
  --allow-exports false \
  --public-network-enabled false \
  --default-action Deny \
  --output none; then
  echo 'Failed to close the authenticated ACR build window.' >&2
  exit 1
fi

if [[ -z "${SERVICE_FRONTEND_NAME:-}" || -z "${AZURE_RESOURCE_GROUP:-}" ]]; then
  echo 'Frontend app name or resource group is missing; authenticated ingress cannot be enabled.' >&2
  exit 1
fi

# The real frontend image and Entra provider are ready, so external ingress can open.
$ingress_enabled='false'
for attempt in $(seq 1 10); do
  if az containerapp ingress enable \
    --name "${SERVICE_FRONTEND_NAME}" \
    --resource-group "${AZURE_RESOURCE_GROUP}" \
    --type external \
    --allow-insecure false \
    --target-port 8000 \
    --transport auto \
    --output none; then
    ingress_enabled='true'
    break
  fi
  if [[ "${attempt}" != '10' ]]; then
    printf 'Container Apps operation is still settling (%s/10); retrying ingress.\n' "${attempt}"
    sleep 15
  fi
done
if [[ "${ingress_enabled}" != 'true' ]]; then
  echo 'Failed to enable authenticated frontend ingress.' >&2
  exit 1
fi

if [[ -z "${SERVICE_FRONTEND_URI:-}" || -z "${MODEL_HEALTH_TOKEN:-}" ]]; then
  echo 'Frontend URI or local model health token is missing; skipping prewarm.' >&2
  exit 0
fi

health_token="${MODEL_HEALTH_TOKEN}"

# Prewarm is best effort: scale-to-zero remains valid when the model is cold.
echo 'Prewarming the inference replica. The first model load can take 10-15 minutes.'
for attempt in $(seq 1 40); do
  status_code="$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 35 \
    --header "Authorization: Bearer ${health_token}" \
    "${SERVICE_FRONTEND_URI}/healthz/model" 2>/dev/null || true)"
  if [[ "${status_code}" == '200' ]]; then
    azd env set MODEL_HEALTH_TOKEN '' >/dev/null
    echo 'Model is warm and serving.'
    exit 0
  fi
  if [[ "${status_code}" == '401' ]]; then
    azd env set MODEL_HEALTH_TOKEN '' >/dev/null
    echo 'Local model health token is stale; cleared it and skipped prewarm.' >&2
    exit 0
  fi
  printf 'Model is still loading (%s/40).\n' "${attempt}"
  sleep 30
done

echo 'Prewarm timed out. Deployment remains valid and the model will start on first use.' >&2
exit 0