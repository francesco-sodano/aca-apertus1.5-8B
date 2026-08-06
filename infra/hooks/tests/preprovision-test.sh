#!/usr/bin/env bash
set -euo pipefail

workspace_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
hook="${workspace_root}/infra/hooks/preprovision.sh"
temp_dir="$(mktemp -d)"
trap 'rm -rf "${temp_dir}"' EXIT

cat >"${temp_dir}/az" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'az %s\n' "$*" >>"${MOCK_LOG}"
if [[ "$*" == *'workload-profile list-supported'* ]]; then
  printf '1\n'
fi
EOF

cat >"${temp_dir}/azd" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'azd %s\n' "$*" >>"${MOCK_LOG}"
EOF

cat >"${temp_dir}/openssl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'generated-secret\n'
EOF

chmod +x "${temp_dir}/az" "${temp_dir}/azd" "${temp_dir}/openssl"

run_hook() {
  local log_file="$1"
  shift
  env \
    -u AZURE_CONTAINER_REGISTRY_NAME \
    -u AZURE_STORAGE_ACCOUNT_NAME \
    -u AZURE_KEY_VAULT_NAME \
    -u AZURE_CONTENT_SAFETY_ACCOUNT_NAME \
    PATH="${temp_dir}:${PATH}" \
    MOCK_LOG="${log_file}" \
    AZURE_ENV_NAME='test-env' \
    APERTUS_MODEL_REVISION='model-revision' \
    ENTRA_CLIENT_ID='00000000-0000-0000-0000-000000000001' \
    ENTRA_TENANT_ID='00000000-0000-0000-0000-000000000002' \
    ALERT_EMAIL='operator@example.com' \
    APPLICATION_SECRETS_READY='true' \
    WEBIQ_SECRET_READY='true' \
    ACCEPT_APERTUS_LICENSE='true' \
    ACCEPT_WEBIQ_TERMS='true' \
    AZURE_LOCATION='swedencentral' \
    "$@" \
    bash "${hook}"
}

ready_log="${temp_dir}/ready.log"
run_hook "${ready_log}"

invalid_environment_log="${temp_dir}/invalid-environment.log"
if run_hook "${invalid_environment_log}" AZURE_ENV_NAME='Invalid_Name'; then
  echo 'Invalid azd environment name was accepted.' >&2
  exit 1
fi

invalid_override_log="${temp_dir}/invalid-override.log"
if run_hook "${invalid_override_log}" AZURE_CONTAINER_REGISTRY_NAME='bad_registry'; then
  echo 'Invalid ACR override was accepted.' >&2
  exit 1
fi

invalid_tag_log="${temp_dir}/invalid-tag.log"
if run_hook "${invalid_tag_log}" APERTUS_IMAGE_TAG_OVERRIDE='bad tag'; then
  echo 'Invalid inference image-tag override was accepted.' >&2
  exit 1
fi

valid_override_log="${temp_dir}/valid-override.log"
run_hook "${valid_override_log}" \
  AZURE_CONTAINER_REGISTRY_NAME='portableacr123' \
  AZURE_STORAGE_ACCOUNT_NAME='portablestorage123' \
  AZURE_KEY_VAULT_NAME='portable-vault-123' \
  AZURE_CONTENT_SAFETY_ACCOUNT_NAME='portable-safety-123'

missing_webiq_log="${temp_dir}/missing-webiq.log"
if run_hook "${missing_webiq_log}" WEBIQ_SECRET_READY='false'; then
  echo 'Missing Web IQ key was accepted before the secret was initialized.' >&2
  exit 1
fi

webiq_migration_log="${temp_dir}/webiq-migration.log"
run_hook "${webiq_migration_log}" WEBIQ_SECRET_READY='false' WEBIQ_API_KEY='test-webiq-key'

webiq_rotation_log="${temp_dir}/webiq-rotation.log"
run_hook "${webiq_rotation_log}" \
  WEBIQ_SECRET_READY='true' \
  ROTATE_WEBIQ_SECRET='true' \
  WEBIQ_API_KEY='rotated-webiq-key'

echo 'preprovision portability tests passed.'