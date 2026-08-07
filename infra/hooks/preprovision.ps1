$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Require-Value([string] $Name) {
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "Required environment value is missing: $Name"
    }
}

function Preserve-AccountNameFromEndpoint(
    [string] $Name,
    [string] $EndpointName
) {
    if (-not [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($Name))) {
        return
    }
    $endpoint = [Environment]::GetEnvironmentVariable($EndpointName)
    if ([string]::IsNullOrWhiteSpace($endpoint)) {
        return
    }
    $accountName = ([Uri]$endpoint).Host.Split('.')[0]
    if (-not [string]::IsNullOrWhiteSpace($accountName)) {
        azd env set $Name $accountName | Out-Null
        Write-Host "Preserved existing $Name value from $EndpointName."
    }
}

function Validate-OptionalName(
    [string] $Name,
    [string] $Pattern,
    [string] $Requirement
) {
    $value = [Environment]::GetEnvironmentVariable($Name)
    if (-not [string]::IsNullOrWhiteSpace($value) -and $value -notmatch $Pattern) {
        throw "$Name is invalid. $Requirement"
    }
}

foreach ($command in @('az', 'azd')) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw "Required command is not installed: $command"
    }
}

Require-Value 'AZURE_ENV_NAME'
Require-Value 'APERTUS_MODEL_REVISION'
Require-Value 'ENTRA_CLIENT_ID'
Require-Value 'ENTRA_TENANT_ID'
Require-Value 'ALERT_EMAIL'

if ($env:AZURE_ENV_NAME -notmatch '^[a-z0-9][a-z0-9-]{3,22}[a-z0-9]$') {
    throw 'AZURE_ENV_NAME must be 5-24 lowercase letters, numbers, or hyphens, and must start and end with a letter or number.'
}

Preserve-AccountNameFromEndpoint 'AZURE_FOUNDRY_ACCOUNT_NAME' 'FOUNDRY_PROJECT_ENDPOINT'
Preserve-AccountNameFromEndpoint 'AZURE_CONTENT_SAFETY_ACCOUNT_NAME' 'CONTENT_SAFETY_ENDPOINT'

Validate-OptionalName 'AZURE_CONTAINER_REGISTRY_NAME' '^[a-z0-9]{5,50}$' 'Use 5-50 lowercase letters or numbers.'
Validate-OptionalName 'AZURE_STORAGE_ACCOUNT_NAME' '^[a-z0-9]{3,24}$' 'Use 3-24 lowercase letters or numbers.'
Validate-OptionalName 'AZURE_KEY_VAULT_NAME' '^[a-z][a-z0-9-]{1,22}[a-z0-9]$' 'Use 3-24 lowercase letters, numbers, or hyphens; start with a letter and end with a letter or number.'
Validate-OptionalName 'AZURE_FOUNDRY_ACCOUNT_NAME' '^[a-z][a-z0-9-]{0,62}[a-z0-9]$' 'Use 2-64 lowercase letters, numbers, or hyphens; start with a letter and end with a letter or number.'
Validate-OptionalName 'AZURE_CONTENT_SAFETY_ACCOUNT_NAME' '^[a-z][a-z0-9-]{0,62}[a-z0-9]$' 'Use 2-64 lowercase letters, numbers, or hyphens; start with a letter and end with a letter or number.'
Validate-OptionalName 'APERTUS_IMAGE_TAG_OVERRIDE' '^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$' 'Use a valid container tag with at most 128 letters, numbers, periods, underscores, or hyphens.'

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