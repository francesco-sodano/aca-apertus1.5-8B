#!/usr/bin/env bash
set -euo pipefail

workspace_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
hook="${workspace_root}/infra/hooks/postprovision.sh"
temp_dir="$(mktemp -d)"
trap 'rm -rf "${temp_dir}"' EXIT

cat >"${temp_dir}/az" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'az %s\n' "$*" >>"${MOCK_LOG}"
if [[ "$*" == *'artifact-streaming operation show'* ]]; then
  operation_count="$(grep -c 'artifact-streaming operation show' "${MOCK_LOG}" || true)"
  if [[ "${MOCK_CONVERSION_EXISTS:-false}" == 'true' || "${operation_count}" -gt 1 ]]; then
    printf '%s\n' "${MOCK_STREAMING_STATUS}"
  else
    exit 1
  fi
fi
if [[ "$*" == *'acr show'* ]]; then
  printf '/subscriptions/test/resourceGroups/test-rg/providers/Microsoft.ContainerRegistry/registries/testregistry\n'
fi
if [[ "$*" == *'role assignment list'* ]]; then
  printf 'AcrPull\n'
fi
if [[ "$*" == *'acr repository list'* ]]; then
  readiness_count="$(grep -c 'acr repository list' "${MOCK_LOG}" || true)"
  if [[ "${readiness_count}" -lt "${MOCK_ACR_READY_AFTER:-1}" ]]; then
    exit 1
  fi
fi
if [[ "$*" == *'acr repository show'* ]]; then
  if [[ "${MOCK_IMAGE_EXISTS:-false}" == 'true' ]]; then
    printf 'sha256:existing\n'
  else
    exit 1
  fi
fi
if [[ "$*" == *'keyvault secret show'* && "${MOCK_SECRETS_EXIST:-false}" == 'true' ]]; then
  printf '/subscriptions/test/secrets/existing\n'
fi
if [[ "$*" == *'cloud show'* ]]; then
  printf 'https://login.microsoftonline.com/\n'
fi
EOF

cat >"${temp_dir}/azd" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'azd %s\n' "$*" >>"${MOCK_LOG}"
EOF

cat >"${temp_dir}/sleep" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'sleep %s\n' "$*" >>"${MOCK_LOG}"
EOF

chmod +x "${temp_dir}/az" "${temp_dir}/azd" "${temp_dir}/sleep"

run_hook() {
  local status="$1"
  local log_file="$2"
  local secrets_exist="${3:-false}"
  local image_exists="${4:-false}"
  local conversion_exists="${5:-false}"
  local acr_ready_after="${6:-1}"
  local image_tag_override="${7-test-tag}"
  PATH="${temp_dir}:${PATH}" \
  MOCK_LOG="${log_file}" \
  MOCK_STREAMING_STATUS="${status}" \
  MOCK_SECRETS_EXIST="${secrets_exist}" \
  MOCK_IMAGE_EXISTS="${image_exists}" \
  MOCK_CONVERSION_EXISTS="${conversion_exists}" \
  MOCK_ACR_READY_AFTER="${acr_ready_after}" \
  AZURE_CONTAINER_REGISTRY_NAME='testregistry' \
  AZURE_CONTAINER_REGISTRY_ENDPOINT='testregistry.azurecr.io' \
  AZURE_ENV_NAME='test-env' \
  AZURE_KEY_VAULT_NAME='test-vault' \
  AZURE_RESOURCE_GROUP='test-rg' \
  SERVICE_FRONTEND_NAME='test-frontend' \
  SERVICE_FRONTEND_URI='https://test-frontend.example.com' \
  SERVICE_INFERENCE_NAME='test-inference' \
  INFERENCE_IDENTITY_RESOURCE_ID='/subscriptions/test/identities/inference' \
  INFERENCE_IDENTITY_PRINCIPAL_ID='00000000-0000-0000-0000-000000000003' \
  FRONTEND_IDENTITY_RESOURCE_ID='/subscriptions/test/identities/frontend' \
  FRONTEND_IDENTITY_PRINCIPAL_ID='00000000-0000-0000-0000-000000000004' \
  HF_TOKEN='test-token' \
  ENTRA_CLIENT_ID='00000000-0000-0000-0000-000000000001' \
  ENTRA_TENANT_ID='00000000-0000-0000-0000-000000000002' \
  ENTRA_CLIENT_SECRET='test-entra-secret' \
  APERTUS_IMAGE_TAG_OVERRIDE="${image_tag_override}" \
  bash "${hook}"
}

failed_log="${temp_dir}/failed.log"
if run_hook 'Failed' "${failed_log}"; then
  echo 'Expected failed artifact conversion to fail the hook.' >&2
  exit 1
fi
if grep -q 'containerapp update' "${failed_log}"; then
  echo 'Inference or frontend image was updated after failed conversion.' >&2
  exit 1
fi
if ! grep -q 'acr update.*allow-exports false.*public-network-enabled false.*default-action Deny' "${failed_log}"; then
  echo 'Failed postprovision did not restore the private ACR posture.' >&2
  exit 1
fi

success_log="${temp_dir}/success.log"
run_hook 'Succeeded' "${success_log}"
rbac_line="$(grep -n 'role assignment list' "${success_log}" | head -n 1 | cut -d: -f1)"
open_line="$(grep -n 'acr update.*public-network-enabled true' "${success_log}" | head -n 1 | cut -d: -f1)"
build_line="$(grep -n 'acr build' "${success_log}" | head -n 1 | cut -d: -f1)"
operation_line="$(grep -n 'artifact-streaming operation show' "${success_log}" | head -n 1 | cut -d: -f1)"
promotion_line="$(grep -n 'containerapp update' "${success_log}" | head -n 1 | cut -d: -f1)"
if [[ -z "${rbac_line}" || -z "${build_line}" || "${rbac_line}" -ge "${build_line}" ]]; then
  echo 'AcrPull propagation was not verified before the image build.' >&2
  exit 1
fi
if [[ -z "${open_line}" || "${open_line}" -ge "${build_line}" ]]; then
  echo 'The authenticated ACR build window was not opened before the image build.' >&2
  exit 1
fi
if [[ -z "${operation_line}" || -z "${promotion_line}" || "${operation_line}" -ge "${promotion_line}" ]]; then
  echo 'Successful conversion was not verified before image promotion.' >&2
  exit 1
fi
if ! grep -q 'auth microsoft update' "${success_log}"; then
  echo 'Microsoft Entra authentication was not configured.' >&2
  exit 1
fi
if grep -q 'ingress enable' "${success_log}"; then
  echo 'Public ingress was enabled before the real frontend image deployment.' >&2
  exit 1
fi

idempotent_log="${temp_dir}/idempotent.log"
run_hook 'Succeeded' "${idempotent_log}" 'true'
if grep -q 'keyvault secret set' "${idempotent_log}"; then
  echo 'Existing Key Vault secrets were replaced during a normal redeployment.' >&2
  exit 1
fi

resume_log="${temp_dir}/resume.log"
run_hook 'Succeeded' "${resume_log}" 'false' 'true'
if grep -q 'acr build' "${resume_log}"; then
  echo 'An existing immutable image was rebuilt instead of reused.' >&2
  exit 1
fi
if ! grep -q 'artifact-streaming create' "${resume_log}" || ! grep -q 'containerapp update' "${resume_log}"; then
  echo 'Resume path did not continue through conversion and promotion.' >&2
  exit 1
fi

conversion_resume_log="${temp_dir}/conversion-resume.log"
run_hook 'Succeeded' "${conversion_resume_log}" 'false' 'true' 'true'
if grep -q 'acr build' "${conversion_resume_log}" || grep -q 'artifact-streaming create' "${conversion_resume_log}"; then
  echo 'Completed image or conversion work was repeated during resume.' >&2
  exit 1
fi
if ! grep -q 'containerapp update' "${conversion_resume_log}"; then
  echo 'Conversion resume path did not continue through promotion.' >&2
  exit 1
fi

automatic_tag_log="${temp_dir}/automatic-tag.log"
run_hook 'Succeeded' "${automatic_tag_log}" 'false' 'false' 'false' '1' ''
inference_source_tag="$(git -C "${workspace_root}" rev-parse --short=12 HEAD:src/inference)"
if ! grep -q "apertus/inference:1.5.0-test-env-${inference_source_tag}" "${automatic_tag_log}"; then
  echo 'Automatic inference tag is not scoped to the azd environment and inference tree.' >&2
  exit 1
fi

propagation_resume_log="${temp_dir}/propagation-resume.log"
run_hook 'Succeeded' "${propagation_resume_log}" 'false' 'true' 'true' '3'
if [[ "$(grep -c 'acr repository list' "${propagation_resume_log}")" -ne 3 ]]; then
  echo 'ACR data-plane readiness was not retried through transient propagation.' >&2
  exit 1
fi
if grep -q 'acr build' "${propagation_resume_log}"; then
  echo 'A transient ACR data-plane delay caused an existing image to be rebuilt.' >&2
  exit 1
fi

echo 'postprovision artifact-streaming tests passed.'