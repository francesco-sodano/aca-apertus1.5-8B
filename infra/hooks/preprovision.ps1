$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Require-Value([string] $Name) {
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "Required environment value is missing: $Name"
    }
}

foreach ($command in @('az', 'azd')) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw "Required command is not installed: $command"
    }
}

Require-Value 'APERTUS_MODEL_REVISION'
Require-Value 'ENTRA_CLIENT_ID'
Require-Value 'ENTRA_TENANT_ID'
Require-Value 'ALERT_EMAIL'

if ($env:APPLICATION_SECRETS_READY -ne 'true' -or $env:ROTATE_APPLICATION_SECRETS -eq 'true') {
    Require-Value 'HF_TOKEN'
    Require-Value 'ENTRA_CLIENT_SECRET'

    if ([string]::IsNullOrWhiteSpace($env:VLLM_API_KEY)) {
        $bytes = [byte[]]::new(32)
        [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
        azd env set VLLM_API_KEY ([Convert]::ToHexString($bytes).ToLowerInvariant()) | Out-Null
    }
    if ([string]::IsNullOrWhiteSpace($env:MODEL_HEALTH_TOKEN)) {
        $bytes = [byte[]]::new(32)
        [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
        azd env set MODEL_HEALTH_TOKEN ([Convert]::ToHexString($bytes).ToLowerInvariant()) | Out-Null
    }
}

if ($env:ACCEPT_APERTUS_LICENSE -ne 'true') {
    throw 'Set ACCEPT_APERTUS_LICENSE=true after accepting the Apertus model terms on Hugging Face.'
}
if ($env:ACCEPT_BING_GROUNDING_TERMS -ne 'true') {
    throw 'Set ACCEPT_BING_GROUNDING_TERMS=true after reviewing the Grounding with Bing data-boundary terms.'
}

$location = if ($env:AZURE_LOCATION) { $env:AZURE_LOCATION } else { 'swedencentral' }
if ($location -ne 'swedencentral') {
    throw 'This template is validated only for AZURE_LOCATION=swedencentral.'
}

$profileCount = az containerapp env workload-profile list-supported `
    --location $location `
    --query "[?name=='Consumption-GPU-NC24-A100'] | length(@)" `
    --output tsv
if ($LASTEXITCODE -ne 0 -or [int]$profileCount -eq 0) {
    throw 'Consumption-GPU-NC24-A100 is not reported as available in Sweden Central.'
}

az acr artifact-streaming --help | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'The installed Azure CLI does not support ACR artifact streaming.'
}
Write-Host 'Preprovision checks passed.'