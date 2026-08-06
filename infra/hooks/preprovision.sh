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
require_value AZURE_ENV_NAME
require_value APERTUS_MODEL_REVISION
require_value ENTRA_CLIENT_ID
require_value ENTRA_TENANT_ID
require_value ALERT_EMAIL

if [[ ! "${AZURE_ENV_NAME}" =~ ^[a-z0-9][a-z0-9-]{3,22}[a-z0-9]$ ]]; then
  echo 'AZURE_ENV_NAME must be 5-24 lowercase letters, numbers, or hyphens, and must start and end with a letter or number.' >&2
  exit 1
fi

validate_optional_name() {
  local name="$1"
  local pattern="$2"
  local requirement="$3"
  [[ -z "${!name:-}" || "${!name}" =~ ${pattern} ]] || {
    printf '%s is invalid. %s\n' "${name}" "${requirement}" >&2
    exit 1
  }
}

validate_optional_name AZURE_CONTAINER_REGISTRY_NAME '^[a-z0-9]{5,50}$' 'Use 5-50 lowercase letters or numbers.'
validate_optional_name AZURE_STORAGE_ACCOUNT_NAME '^[a-z0-9]{3,24}$' 'Use 3-24 lowercase letters or numbers.'
validate_optional_name AZURE_KEY_VAULT_NAME '^[a-z][a-z0-9-]{1,22}[a-z0-9]$' 'Use 3-24 lowercase letters, numbers, or hyphens; start with a letter and end with a letter or number.'
validate_optional_name AZURE_CONTENT_SAFETY_ACCOUNT_NAME '^[a-z][a-z0-9-]{0,62}[a-z0-9]$' 'Use 2-64 lowercase letters, numbers, or hyphens; start with a letter and end with a letter or number.'
validate_optional_name APERTUS_IMAGE_TAG_OVERRIDE '^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$' 'Use a valid container tag with at most 128 letters, numbers, periods, underscores, or hyphens.'

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

if [[ "${ROTATE_VLLM_SECRET:-false}" == 'true' && -z "${VLLM_API_KEY:-}" ]]; then
  azd env set VLLM_API_KEY "$(openssl rand -hex 32)" >/dev/null
fi

if [[ "${WEBIQ_SECRET_READY:-false}" != 'true' || "${ROTATE_APPLICATION_SECRETS:-false}" == 'true' || "${ROTATE_WEBIQ_SECRET:-false}" == 'true' ]]; then
  require_value WEBIQ_API_KEY
fi

if [[ "${ACCEPT_APERTUS_LICENSE:-false}" != 'true' ]]; then
  echo 'Set ACCEPT_APERTUS_LICENSE=true after accepting the Apertus model terms on Hugging Face.' >&2
  exit 1
fi

if [[ "${ACCEPT_WEBIQ_TERMS:-false}" != 'true' ]]; then
  echo 'Set ACCEPT_WEBIQ_TERMS=true after reviewing the Microsoft Web IQ terms for your enabled profile.' >&2
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