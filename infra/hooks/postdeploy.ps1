$ErrorActionPreference = 'Continue'
Set-StrictMode -Version Latest

if ([string]::IsNullOrWhiteSpace($env:AZURE_CONTAINER_REGISTRY_NAME)) {
    throw 'Container Registry name is missing; the deployment build window cannot be closed.'
}

az acr update --name $env:AZURE_CONTAINER_REGISTRY_NAME `
    --allow-exports false --public-network-enabled false --default-action Deny --output none
if ($LASTEXITCODE -ne 0) { throw 'Failed to close the authenticated ACR build window.' }

if ([string]::IsNullOrWhiteSpace($env:SERVICE_FRONTEND_NAME) -or
    [string]::IsNullOrWhiteSpace($env:AZURE_RESOURCE_GROUP)) {
    throw 'Frontend app name or resource group is missing; authenticated ingress cannot be enabled.'
}

az containerapp ingress enable --name $env:SERVICE_FRONTEND_NAME --resource-group $env:AZURE_RESOURCE_GROUP `
    --type external --allow-insecure false --target-port 8000 --transport auto --output none
if ($LASTEXITCODE -ne 0) { throw 'Failed to enable authenticated frontend ingress.' }

if ([string]::IsNullOrWhiteSpace($env:SERVICE_FRONTEND_URI) -or
    [string]::IsNullOrWhiteSpace($env:MODEL_HEALTH_TOKEN)) {
    Write-Warning 'Frontend URI or local model health token is missing; skipping prewarm.'
    exit 0
}

$healthToken = $env:MODEL_HEALTH_TOKEN

Write-Host 'Prewarming the inference replica. The first model load can take 10-15 minutes.'
$headers = @{ Authorization = "Bearer $healthToken" }
for ($attempt = 1; $attempt -le 40; $attempt++) {
    try {
        Invoke-RestMethod -Uri "$($env:SERVICE_FRONTEND_URI)/healthz/model" -Headers $headers -TimeoutSec 35 | Out-Null
        azd env set MODEL_HEALTH_TOKEN '' | Out-Null
        Write-Host 'Model is warm and serving.'
        exit 0
    }
    catch {
        Write-Host "Model is still loading ($attempt/40)."
        Start-Sleep -Seconds 30
    }
}

Write-Warning 'Prewarm timed out. Deployment remains valid and the model will start on first use.'
exit 0