$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

foreach ($name in @(
    'AZURE_CONTAINER_REGISTRY_NAME',
    'AZURE_CONTAINER_REGISTRY_ENDPOINT',
    'AZURE_KEY_VAULT_NAME',
    'AZURE_RESOURCE_GROUP',
    'FRONTEND_IDENTITY_PRINCIPAL_ID',
    'INFERENCE_IDENTITY_PRINCIPAL_ID',
    'SERVICE_FRONTEND_NAME',
    'SERVICE_FRONTEND_URI',
    'SERVICE_INFERENCE_NAME',
    'AZURE_ENV_NAME',
    'ENTRA_CLIENT_ID',
    'ENTRA_TENANT_ID'
)) {
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) {
        throw "Missing required deployment value: $name"
    }
}

$inferenceSourceTag = (git rev-parse --short=12 HEAD:src/inference 2>$null)
if ($LASTEXITCODE -ne 0) { $inferenceSourceTag = 'local' }
$imageTag = if ($env:APERTUS_IMAGE_TAG_OVERRIDE) { $env:APERTUS_IMAGE_TAG_OVERRIDE } else { "1.5.0-$($env:AZURE_ENV_NAME)-$inferenceSourceTag" }
$inferenceRepository = 'apertus/inference'
$acrBuildWindowOpen = $false

# Bootstrap secrets are now in Key Vault; remove source copies from local azd state.
azd env set APPLICATION_SECRETS_READY true | Out-Null
azd env set HF_TOKEN '' | Out-Null
azd env set ENTRA_CLIENT_SECRET '' | Out-Null
azd env set VLLM_API_KEY '' | Out-Null

$acrResourceId = az acr show --name $env:AZURE_CONTAINER_REGISTRY_NAME --query id --output tsv
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($acrResourceId)) {
    throw 'Could not resolve the ACR resource ID for the RBAC gate.'
}

# Do not push private images until both runtime identities can pull from ACR.
foreach ($identity in @(
    @{ Name = 'frontend'; PrincipalId = $env:FRONTEND_IDENTITY_PRINCIPAL_ID },
    @{ Name = 'inference'; PrincipalId = $env:INFERENCE_IDENTITY_PRINCIPAL_ID }
)) {
    $role = $null
    for ($attempt = 1; $attempt -le 10; $attempt++) {
        $role = az role assignment list --scope $acrResourceId `
            --assignee-object-id $identity.PrincipalId `
            --query "[?roleDefinitionName=='AcrPull'].roleDefinitionName" --output tsv 2>$null
        if ($role -eq 'AcrPull') { break }
        if ($attempt -lt 10) { Start-Sleep -Seconds 30 }
    }
    if ($role -ne 'AcrPull') {
        throw "AcrPull did not propagate for the $($identity.Name) identity."
    }
    Write-Host "AcrPull confirmed for the $($identity.Name) identity."
}

try {
    # ACR Tasks is Microsoft hosted, so the private registry opens only for the build.
    Write-Host 'Opening the authenticated ACR build window.'
    az acr update --name $env:AZURE_CONTAINER_REGISTRY_NAME `
        --allow-exports true --public-network-enabled true --default-action Allow --output none
    if ($LASTEXITCODE -ne 0) { throw 'Could not open the authenticated ACR build window.' }
    $acrBuildWindowOpen = $true

$acrDataPlaneReady = $false
for ($attempt = 1; $attempt -le 6; $attempt++) {
    az acr repository list --name $env:AZURE_CONTAINER_REGISTRY_NAME `
        --top 1 --output none 2>$null
    if ($LASTEXITCODE -eq 0) {
        $acrDataPlaneReady = $true
        break
    }
    if ($attempt -lt 6) {
        Write-Host "ACR data plane is still propagating ($attempt/6); retrying."
        Start-Sleep -Seconds 30
    }
}
if (-not $acrDataPlaneReady) { throw 'ACR data plane did not become reachable.' }

Write-Host "Building the immutable inference image in $($env:AZURE_CONTAINER_REGISTRY_NAME)..."
$imageDigest = az acr repository show --name $env:AZURE_CONTAINER_REGISTRY_NAME `
    --image "${inferenceRepository}:${imageTag}" --query digest --output tsv 2>$null
if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($imageDigest)) {
    Write-Host "Reusing existing immutable inference image ${inferenceRepository}:${imageTag}."
}
else {
    $buildSucceeded = $false
    for ($attempt = 1; $attempt -le 6; $attempt++) {
        az acr build --registry $env:AZURE_CONTAINER_REGISTRY_NAME --image "${inferenceRepository}:${imageTag}" src/inference
        if ($LASTEXITCODE -eq 0) {
            $buildSucceeded = $true
            break
        }
        if ($attempt -lt 6) {
            Write-Host "ACR build plane is still propagating ($attempt/6); retrying."
            Start-Sleep -Seconds 30
        }
    }
    if (-not $buildSucceeded) { throw 'Inference image build failed.' }
}

# The GPU app is promoted only after the exact immutable tag is streamable.
$streamingStatus = az acr artifact-streaming operation show `
    --name $env:AZURE_CONTAINER_REGISTRY_NAME `
    --image "${inferenceRepository}:${imageTag}" `
    --query 'status' `
    --output tsv 2>$null
if ($streamingStatus -eq 'Succeeded') {
    Write-Host 'Reusing successful artifact-stream conversion.'
}
else {
    az acr artifact-streaming update --name $env:AZURE_CONTAINER_REGISTRY_NAME --repository $inferenceRepository --enable-streaming true --output none
    if ($LASTEXITCODE -ne 0) { throw 'Could not enable artifact-stream auto-conversion.' }
    az acr artifact-streaming create --name $env:AZURE_CONTAINER_REGISTRY_NAME --image "${inferenceRepository}:${imageTag}" --output none
    if ($LASTEXITCODE -ne 0) { throw 'Could not start artifact-stream conversion.' }

    $streamingStatus = 'Unknown'
    for ($attempt = 1; $attempt -le 90; $attempt++) {
        $streamingStatus = az acr artifact-streaming operation show `
            --name $env:AZURE_CONTAINER_REGISTRY_NAME `
            --image "${inferenceRepository}:${imageTag}" `
            --query 'status' `
            --output tsv 2>$null
        Write-Host "Artifact streaming status: $streamingStatus"
        if ($streamingStatus -eq 'Succeeded') { break }
        if ($streamingStatus -in @('Failed', 'Canceled')) {
            throw 'Artifact-stream conversion failed; inference app will not be updated.'
        }
        Start-Sleep -Seconds 20
    }
}
if ($streamingStatus -ne 'Succeeded') {
    throw 'Artifact-stream conversion timed out; inference app will not be updated.'
}

# Container Apps resolves these versionless references through managed identity.
$keyVaultUri = "https://$($env:AZURE_KEY_VAULT_NAME).vault.azure.net/secrets"
$inferenceIdentity = if ($env:INFERENCE_IDENTITY_RESOURCE_ID) { $env:INFERENCE_IDENTITY_RESOURCE_ID } else { 'system' }
$frontendIdentity = if ($env:FRONTEND_IDENTITY_RESOURCE_ID) { $env:FRONTEND_IDENTITY_RESOURCE_ID } else { 'system' }

az containerapp secret set --name $env:SERVICE_INFERENCE_NAME --resource-group $env:AZURE_RESOURCE_GROUP --secrets `
    "hugging-face-token=keyvaultref:${keyVaultUri}/hugging-face-token,identityref:${inferenceIdentity}" `
    "vllm-api-key=keyvaultref:${keyVaultUri}/vllm-api-key,identityref:${inferenceIdentity}" --output none
if ($LASTEXITCODE -ne 0) { throw 'Could not configure inference secrets.' }

az containerapp secret set --name $env:SERVICE_FRONTEND_NAME --resource-group $env:AZURE_RESOURCE_GROUP --secrets `
    "vllm-api-key=keyvaultref:${keyVaultUri}/vllm-api-key,identityref:${frontendIdentity}" `
    "model-health-token=keyvaultref:${keyVaultUri}/model-health-token,identityref:${frontendIdentity}" `
    "entra-client-secret=keyvaultref:${keyVaultUri}/entra-client-secret,identityref:${frontendIdentity}" --output none
if ($LASTEXITCODE -ne 0) { throw 'Could not configure frontend secrets.' }

# Preserve existing redirects while registering the generated Container Apps callback.
$callbackUri = "$($env:SERVICE_FRONTEND_URI)/.auth/login/aad/callback"
$existingRedirectUris = @(
    az ad app show --id $env:ENTRA_CLIENT_ID --query web.redirectUris --output tsv
) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) -and $_ -ne $callbackUri }
if ($LASTEXITCODE -ne 0) { throw 'Could not read the Microsoft Entra app registration.' }
$redirectUris = @($callbackUri) + $existingRedirectUris
az ad app update --id $env:ENTRA_CLIENT_ID --enable-id-token-issuance true `
    --web-redirect-uris $redirectUris --output none
if ($LASTEXITCODE -ne 0) { throw 'Could not register the Container Apps authentication callback URI.' }

az containerapp auth microsoft update --name $env:SERVICE_FRONTEND_NAME --resource-group $env:AZURE_RESOURCE_GROUP `
    --client-id $env:ENTRA_CLIENT_ID --client-secret-name entra-client-secret `
    --tenant-id $env:ENTRA_TENANT_ID `
    --allowed-audiences $env:ENTRA_CLIENT_ID --yes --output none
if ($LASTEXITCODE -ne 0) { throw 'Could not configure the Microsoft Entra identity provider.' }

az containerapp auth update --name $env:SERVICE_FRONTEND_NAME --resource-group $env:AZURE_RESOURCE_GROUP `
    --enabled true --unauthenticated-client-action RedirectToLoginPage `
    --redirect-provider AzureActiveDirectory --require-https true --token-store false `
    --yes --output none
if ($LASTEXITCODE -ne 0) { throw 'Could not enforce Container Apps authentication.' }

# These updates create revisions only after artifacts, secrets, and auth are ready.
Write-Host 'Artifact conversion succeeded; promoting inference image.'
az containerapp update --name $env:SERVICE_INFERENCE_NAME --resource-group $env:AZURE_RESOURCE_GROUP `
    --container-name inference `
    --image "$($env:AZURE_CONTAINER_REGISTRY_ENDPOINT)/${inferenceRepository}:${imageTag}" `
    --set-env-vars 'HF_TOKEN=secretref:hugging-face-token' 'VLLM_API_KEY=secretref:vllm-api-key' --output none
if ($LASTEXITCODE -ne 0) { throw 'Inference image promotion failed.' }

az containerapp update --name $env:SERVICE_FRONTEND_NAME --resource-group $env:AZURE_RESOURCE_GROUP `
    --container-name frontend `
    --set-env-vars 'VLLM_API_KEY=secretref:vllm-api-key' 'MODEL_HEALTH_TOKEN=secretref:model-health-token' --output none
if ($LASTEXITCODE -ne 0) { throw 'Frontend secret environment configuration failed.' }

    azd env set APERTUS_IMAGE_TAG '' | Out-Null
    azd env set ROTATE_APPLICATION_SECRETS false | Out-Null
    azd env set ROTATE_VLLM_SECRET false | Out-Null
    Write-Host 'Inference image promoted and frontend secrets configured successfully.'
}
catch {
    $failure = $_
    if ($acrBuildWindowOpen) {
        Write-Warning 'Postprovision failed; restoring the private ACR posture.'
        $previousPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        az acr update --name $env:AZURE_CONTAINER_REGISTRY_NAME `
            --allow-exports false --public-network-enabled false --default-action Deny --output none
        $cleanupExitCode = $LASTEXITCODE
        $ErrorActionPreference = $previousPreference
        if ($cleanupExitCode -ne 0) {
            Write-Warning 'CRITICAL: failed to restore the private ACR posture.'
        }
    }
    throw $failure
}
