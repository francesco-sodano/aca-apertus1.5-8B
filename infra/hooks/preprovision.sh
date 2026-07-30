#!/usr/bin/env bash
set -euo pipefail

require_value() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    printf 'Required environment value is missing: %s\n' "${name}" >&2
    exit 1
  fi
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    printf 'Required command is not installed: %s\n' "$1" >&2
    exit 1
  }
}

require_command az
require_command azd
require_command openssl
require_value APERTUS_MODEL_REVISION
require_value ENTRA_CLIENT_ID
require_value ENTRA_TENANT_ID
require_value ALERT_EMAIL

if [[ "${APPLICATION_SECRETS_READY:-false}" != 'true' || "${ROTATE_APPLICATION_SECRETS:-false}" == 'true' ]]; then
  require_value HF_TOKEN
  require_value ENTRA_CLIENT_SECRET
  if [[ -z "${VLLM_API_KEY:-}" ]]; then
    azd env set VLLM_API_KEY "$(openssl rand -hex 32)" >/dev/null
  fi
  if [[ -z "${MODEL_HEALTH_TOKEN:-}" ]]; then
    azd env set MODEL_HEALTH_TOKEN "$(openssl rand -hex 32)" >/dev/null
  fi
fi

if [[ "${ACCEPT_APERTUS_LICENSE:-false}" != 'true' ]]; then
  echo 'Set ACCEPT_APERTUS_LICENSE=true after accepting the Apertus model terms on Hugging Face.' >&2
  exit 1
fi

if [[ "${ACCEPT_BING_GROUNDING_TERMS:-false}" != 'true' ]]; then
  echo 'Set ACCEPT_BING_GROUNDING_TERMS=true after reviewing the Grounding with Bing data-boundary terms.' >&2
  exit 1
fi

location="${AZURE_LOCATION:-swedencentral}"
if [[ "${location}" != 'swedencentral' ]]; then
  echo 'This template is validated only for AZURE_LOCATION=swedencentral.' >&2
  exit 1
fi

profile_count="$(az containerapp env workload-profile list-supported \
  --location "${location}" \
  --query "[?name=='Consumption-GPU-NC24-A100'] | length(@)" \
  --output tsv)"
if [[ "${profile_count}" == '0' ]]; then
  echo 'Consumption-GPU-NC24-A100 is not reported as available in Sweden Central.' >&2
  exit 1
fi

az acr artifact-streaming --help >/dev/null
echo 'Preprovision checks passed.'