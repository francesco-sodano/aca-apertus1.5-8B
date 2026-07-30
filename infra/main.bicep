targetScope = 'resourceGroup'

@description('Azure region. Apertus serverless A100 and all AI dependencies are validated for Sweden Central.')
param location string = 'swedencentral'

@minLength(5)
@maxLength(24)
@description('Short, lowercase deployment identifier used to create globally unique resource names.')
param environmentName string

@description('Object ID of the principal running azd. It receives permission to create deployment secrets in Key Vault.')
param deployerPrincipalId string

@description('Pinned Apertus model revision from Hugging Face. Set this before deployment.')
param modelRevision string

@description('Set by the deployment hook after Key Vault application secrets exist.')
param applicationSecretsReady bool = false

@description('Replace all application secrets through secure ARM parameters during this deployment.')
param rotateApplicationSecrets bool = false

@minLength(36)
@description('Application (client) ID of the existing single-tenant Entra app registration used by Container Apps Easy Auth.')
param entraClientId string

@minLength(36)
@description('Microsoft Entra tenant ID that owns the frontend app registration.')
param entraTenantId string

@minLength(3)
@description('Operations email that receives Azure Monitor and budget notifications.')
param alertEmail string

@minValue(1)
@description('Monthly resource-group budget in the subscription billing currency.')
param monthlyBudgetAmount int = 500

@minValue(1)
@description('Data Zone Standard capacity for the Foundry grounding model deployment.')
param groundingModelCapacity int = 10

@description('Tags applied to all resources.')
param tags object = {}

@minLength(5)
@maxLength(50)
@description('Globally unique Azure Container Registry name.')
param containerRegistryName string = 'crapertusdev'

@minLength(3)
@maxLength(24)
@description('Globally unique model-cache storage account name.')
param storageAccountName string = 'stapertusdev'

@minLength(3)
@maxLength(24)
@description('Globally unique Key Vault name.')
param keyVaultName string = 'kv-apertus-dev'

@minLength(2)
@maxLength(64)
@description('Globally unique Microsoft Foundry account and custom subdomain name.')
param foundryAccountName string = 'fdry-apertus-dev'

@minLength(2)
@maxLength(64)
@description('Globally unique Azure AI Content Safety account and custom subdomain name.')
param contentSafetyAccountName string = 'cs-apertus-dev'

@secure()
@description('Read-only Hugging Face token used for the gated Apertus model.')
param huggingFaceToken string = ''

@secure()
@description('Generated internal API key for the vLLM endpoint.')
param vllmApiKey string = ''

@secure()
@description('Generated operator token for model health and prewarm checks.')
param modelHealthToken string = ''

@secure()
@description('Client secret for the existing single-tenant frontend Entra application.')
param entraClientSecret string = ''

var uniqueSuffix = uniqueString(subscription().subscriptionId, resourceGroup().id, environmentName)
var compactSuffix = take(uniqueSuffix, 8)

var logAnalyticsName = take('log-${environmentName}', 63)
var appInsightsName = take('appi-${environmentName}', 260)
var frontendIdentityName = take('id-${environmentName}-frontend', 128)
var inferenceIdentityName = take('id-${environmentName}-inference', 128)
var foundryProjectName = take('proj-${environmentName}-grounding', 64)
var managedEnvironmentName = take('cae-${environmentName}', 32)
var frontendAppName = take('ca-${environmentName}-frontend', 32)
var inferenceAppName = take('ca-${environmentName}-inference', 32)
var virtualNetworkName = take('vnet-${environmentName}', 64)
var modelShareName = 'model-cache'
var modelStorageName = 'model-cache'
var groundingDeploymentName = 'gpt-5-mini'
var bootstrapImage = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest@sha256:e9b3e7c34664c7cffd7144864b0e4eec369bfde80068f9095dc63b37058bec48'
var mcapsDiagnosticStorageName = toLower('mcaps${substring(replace(subscription().subscriptionId, '-', ''), 16)}')

var commonTags = union(tags, {
  'azd-env-name': environmentName
  workload: 'apertus-1.5-8b'
  'managed-by': 'azd'
})

module privateNetwork './modules/network/private-network.bicep' = {
  name: 'private-network'
  params: {
    location: location
    name: virtualNetworkName
    tags: commonTags
  }
}

module frontendIdentity 'br/public:avm/res/managed-identity/user-assigned-identity:0.6.0' = {
  name: 'frontend-identity'
  params: {
    name: frontendIdentityName
    location: location
    tags: commonTags
  }
}

module inferenceIdentity 'br/public:avm/res/managed-identity/user-assigned-identity:0.6.0' = {
  name: 'inference-identity'
  params: {
    name: inferenceIdentityName
    location: location
    tags: commonTags
  }
}

module logAnalytics 'br/public:avm/res/operational-insights/workspace:0.16.0' = {
  name: 'log-analytics'
  params: {
    name: logAnalyticsName
    location: location
    dataRetention: 30
    tags: commonTags
  }
}

module applicationInsights 'br/public:avm/res/insights/component:0.8.0' = {
  name: 'application-insights'
  params: {
    name: appInsightsName
    location: location
    workspaceResourceId: logAnalytics.outputs.resourceId
    applicationType: 'web'
    tags: commonTags
  }
}

module registry 'br/public:avm/res/container-registry/registry:0.12.0' = {
  name: 'container-registry'
  params: {
    name: containerRegistryName
    location: location
    acrAdminUserEnabled: false
    acrSku: 'Premium'
    anonymousPullEnabled: false
    azureADAuthenticationAsArmPolicyStatus: 'enabled'
    exportPolicyStatus: 'disabled'
    publicNetworkAccess: 'Disabled'
    retentionPolicyDays: 15
    retentionPolicyStatus: 'enabled'
    roleAssignments: [
      {
        principalId: deployerPrincipalId
        principalType: 'User'
        roleDefinitionIdOrName: 'AcrPush'
      }
      {
        principalId: frontendIdentity.outputs.principalId
        principalType: 'ServicePrincipal'
        roleDefinitionIdOrName: 'AcrPull'
      }
      {
        principalId: inferenceIdentity.outputs.principalId
        principalType: 'ServicePrincipal'
        roleDefinitionIdOrName: 'AcrPull'
      }
    ]
    diagnosticSettings: [
      {
        workspaceResourceId: logAnalytics.outputs.resourceId
      }
    ]
    tags: commonTags
  }
}

module registryPrivateEndpoint './modules/network/private-endpoint.bicep' = {
  name: 'registry-private-endpoint'
  params: {
    location: location
    name: take('pep-${environmentName}-acr', 80)
    privateDnsZoneResourceIds: [
      privateNetwork.outputs.privateDnsZoneResourceIds.acr
    ]
    service: 'registry'
    subnetResourceId: privateNetwork.outputs.privateEndpointSubnetResourceId
    targetResourceId: registry.outputs.resourceId
    tags: commonTags
  }
}

module keyVault 'br/public:avm/res/key-vault/vault:0.14.0' = {
  name: 'key-vault'
  params: {
    name: keyVaultName
    location: location
    enablePurgeProtection: true
    enableRbacAuthorization: true
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Deny'
    }
    publicNetworkAccess: 'Disabled'
    roleAssignments: [
      {
        principalId: deployerPrincipalId
        roleDefinitionIdOrName: 'Key Vault Secrets Officer'
      }
      {
        principalId: frontendIdentity.outputs.principalId
        principalType: 'ServicePrincipal'
        roleDefinitionIdOrName: 'Key Vault Secrets User'
      }
      {
        principalId: inferenceIdentity.outputs.principalId
        principalType: 'ServicePrincipal'
        roleDefinitionIdOrName: 'Key Vault Secrets User'
      }
    ]
    diagnosticSettings: [
      {
        workspaceResourceId: logAnalytics.outputs.resourceId
      }
    ]
    secrets: applicationSecretsReady && !rotateApplicationSecrets ? [] : [
      {
        contentType: 'Hugging Face read token'
        name: 'hugging-face-token'
        value: huggingFaceToken
      }
      {
        contentType: 'Internal vLLM API key'
        name: 'vllm-api-key'
        value: vllmApiKey
      }
      {
        contentType: 'Model health endpoint token'
        name: 'model-health-token'
        value: modelHealthToken
      }
      {
        contentType: 'Container Apps Easy Auth client secret'
        name: 'entra-client-secret'
        value: entraClientSecret
      }
    ]
    tags: commonTags
  }
}

module keyVaultPrivateEndpoint './modules/network/private-endpoint.bicep' = {
  name: 'key-vault-private-endpoint'
  params: {
    location: location
    name: take('pep-${environmentName}-kv', 80)
    privateDnsZoneResourceIds: [
      privateNetwork.outputs.privateDnsZoneResourceIds.keyVault
    ]
    service: 'vault'
    subnetResourceId: privateNetwork.outputs.privateEndpointSubnetResourceId
    targetResourceId: keyVault.outputs.resourceId
    tags: commonTags
  }
}

module storage 'br/public:avm/res/storage/storage-account:0.33.0' = {
  name: 'model-storage'
  params: {
    name: storageAccountName
    location: location
    kind: 'FileStorage'
    skuName: 'Premium_LRS'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    networkAcls: {
      bypass: 'None'
      defaultAction: 'Deny'
    }
    publicNetworkAccess: 'Disabled'
    requireInfrastructureEncryption: true
    supportsHttpsTrafficOnly: false
    fileServices: {
      shares: [
        {
          accessTier: 'Premium'
          enabledProtocols: 'NFS'
          name: modelShareName
          rootSquash: 'NoRootSquash'
          shareQuota: 100
        }
      ]
    }
    diagnosticSettings: [
      {
        workspaceResourceId: logAnalytics.outputs.resourceId
      }
    ]
    tags: commonTags
  }
}

module storagePrivateEndpoint './modules/network/private-endpoint.bicep' = {
  name: 'storage-private-endpoint'
  params: {
    location: location
    name: take('pep-${environmentName}-file', 80)
    privateDnsZoneResourceIds: [
      privateNetwork.outputs.privateDnsZoneResourceIds.storageFile
    ]
    service: 'file'
    subnetResourceId: privateNetwork.outputs.privateEndpointSubnetResourceId
    targetResourceId: storage.outputs.resourceId
    tags: commonTags
  }
}

module aiServices 'br/public:avm/res/cognitive-services/account:0.17.0' = {
  name: 'foundry-account'
  params: {
    kind: 'AIServices'
    name: foundryAccountName
    location: location
    allowProjectManagement: true
    customSubDomainName: foundryAccountName
    disableLocalAuth: true
    publicNetworkAccess: 'Disabled'
    restrictOutboundNetworkAccess: false
    deployments: [
      {
        model: {
          format: 'OpenAI'
          name: groundingDeploymentName
          version: '2025-08-07'
        }
        name: groundingDeploymentName
        sku: {
          capacity: groundingModelCapacity
          name: 'DataZoneStandard'
        }
        versionUpgradeOption: 'NoAutoUpgrade'
      }
    ]
    roleAssignments: [
      {
        principalId: frontendIdentity.outputs.principalId
        principalType: 'ServicePrincipal'
        roleDefinitionIdOrName: '53ca6127-db72-4b80-b1b0-d745d6d5456d'
      }
    ]
    diagnosticSettings: [
      {
        workspaceResourceId: logAnalytics.outputs.resourceId
      }
    ]
    tags: commonTags
  }
}

module aiServicesPrivateEndpoint './modules/network/private-endpoint.bicep' = {
  name: 'foundry-private-endpoint'
  params: {
    location: location
    name: take('pep-${environmentName}-foundry', 80)
    privateDnsZoneResourceIds: [
      privateNetwork.outputs.privateDnsZoneResourceIds.cognitiveServices
      privateNetwork.outputs.privateDnsZoneResourceIds.openAI
      privateNetwork.outputs.privateDnsZoneResourceIds.aiServices
    ]
    service: 'account'
    subnetResourceId: privateNetwork.outputs.privateEndpointSubnetResourceId
    targetResourceId: aiServices.outputs.resourceId
    tags: commonTags
  }
}

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: foundryAccountName
}

resource foundryProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: foundryAccount
  name: foundryProjectName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    description: 'Mandatory Bing-backed grounding for Apertus requests.'
    displayName: 'Apertus grounding'
  }
  dependsOn: [
    aiServices
  ]
}

resource foundryProjectUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryAccount.id, foundryProject.id, 'Foundry User')
  scope: foundryAccount
  properties: {
    principalId: foundryProject.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '53ca6127-db72-4b80-b1b0-d745d6d5456d'
    )
  }
}

resource mcapsDiagnosticStorage 'Microsoft.Storage/storageAccounts@2023-01-01' existing = {
  name: mcapsDiagnosticStorageName
  scope: resourceGroup('McapsGovernance')
}

resource foundryProjectDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'apertus-project-diagnostics'
  scope: foundryProject
  properties: {
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
    storageAccountId: mcapsDiagnosticStorage.id
  }
}

module contentSafety 'br/public:avm/res/cognitive-services/account:0.17.0' = {
  name: 'content-safety'
  params: {
    kind: 'ContentSafety'
    name: contentSafetyAccountName
    location: location
    customSubDomainName: contentSafetyAccountName
    disableLocalAuth: true
    publicNetworkAccess: 'Disabled'
    restrictOutboundNetworkAccess: false
    roleAssignments: [
      {
        principalId: frontendIdentity.outputs.principalId
        principalType: 'ServicePrincipal'
        roleDefinitionIdOrName: 'Cognitive Services User'
      }
    ]
    diagnosticSettings: [
      {
        workspaceResourceId: logAnalytics.outputs.resourceId
      }
    ]
    sku: 'S0'
    tags: commonTags
  }
}

module contentSafetyPrivateEndpoint './modules/network/private-endpoint.bicep' = {
  name: 'content-safety-private-endpoint'
  params: {
    location: location
    name: take('pep-${environmentName}-content-safety', 80)
    privateDnsZoneResourceIds: [
      privateNetwork.outputs.privateDnsZoneResourceIds.cognitiveServices
    ]
    service: 'account'
    subnetResourceId: privateNetwork.outputs.privateEndpointSubnetResourceId
    targetResourceId: contentSafety.outputs.resourceId
    tags: commonTags
  }
}

module managedEnvironment 'br/public:avm/res/app/managed-environment:0.15.0' = {
  name: 'container-apps-environment'
  params: {
    name: managedEnvironmentName
    location: location
    appInsightsConnectionString: applicationInsights.outputs.connectionString
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsWorkspaceResourceId: logAnalytics.outputs.resourceId
    }
    infrastructureSubnetResourceId: privateNetwork.outputs.infrastructureSubnetResourceId
    internal: false
    peerTrafficEncryption: true
    publicNetworkAccess: 'Enabled'
    storages: [
      {
        accessMode: 'ReadWrite'
        kind: 'NFS'
        name: modelStorageName
        storageAccountName: storage.outputs.name
      }
    ]
    workloadProfiles: [
      {
        name: 'gpu-a100'
        workloadProfileType: 'Consumption-GPU-NC24-A100'
      }
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
    zoneRedundant: false
    tags: commonTags
  }
  dependsOn: [
    storagePrivateEndpoint
  ]
}

resource existingInferenceApp 'Microsoft.App/containerApps@2025-01-01' existing = if (applicationSecretsReady) {
  name: inferenceAppName
}

resource existingFrontendApp 'Microsoft.App/containerApps@2025-01-01' existing = if (applicationSecretsReady) {
  name: frontendAppName
}

module inferenceApp './modules/app/inference-container-app.bicep' = {
  name: 'inference-container-app'
  params: {
    name: inferenceAppName
    location: location
    environmentResourceId: managedEnvironment.outputs.resourceId
    identityResourceId: inferenceIdentity.outputs.resourceId
    registryServer: registry.outputs.loginServer
    image: applicationSecretsReady
      ? existingInferenceApp!.properties.template.containers[0].image
      : bootstrapImage
    keyVaultUri: keyVault.outputs.uri
    modelRevision: modelRevision
    modelStorageName: modelStorageName
    configureApplicationSecrets: applicationSecretsReady
    tags: commonTags
  }
}

module frontendApp './modules/app/frontend-container-app.bicep' = {
  name: 'frontend-container-app'
  params: {
    name: frontendAppName
    location: location
    environmentResourceId: managedEnvironment.outputs.resourceId
    identityResourceId: frontendIdentity.outputs.resourceId
    registryServer: registry.outputs.loginServer
    image: applicationSecretsReady
      ? existingFrontendApp!.properties.template.containers[0].image
      : bootstrapImage
    keyVaultUri: keyVault.outputs.uri
    modelEndpoint: 'https://${inferenceApp.outputs.fqdn}/v1'
    contentSafetyEndpoint: contentSafety.outputs.endpoint
    foundryProjectEndpoint: 'https://${foundryAccountName}.services.ai.azure.com/api/projects/${foundryProjectName}'
    appInsightsConnectionString: applicationInsights.outputs.connectionString
    configureApplicationSecrets: applicationSecretsReady
    entraClientId: entraClientId
    entraTenantId: entraTenantId
    tags: commonTags
  }
}

module operationsActionGroup 'br/public:avm/res/insights/action-group:0.8.0' = {
  name: 'operations-action-group'
  params: {
    groupShortName: take('ag${compactSuffix}', 12)
    name: take('ag-${environmentName}-${compactSuffix}', 260)
    emailReceivers: [
      {
        emailAddress: alertEmail
        name: 'ApertusOperations'
        useCommonAlertSchema: true
      }
    ]
    tags: commonTags
  }
}

module monthlyBudget 'br/public:avm/res/consumption/budget/rg-scope:0.1.0' = {
  name: 'monthly-budget'
  params: {
    amount: monthlyBudgetAmount
    name: take('budget-${environmentName}', 63)
    actionGroups: [
      operationsActionGroup.outputs.resourceId
    ]
    contactEmails: [
      alertEmail
    ]
    thresholds: [
      70
      90
      100
    ]
  }
}

module frontendTimeoutAlert 'br/public:avm/res/insights/metric-alert:0.4.0' = {
  name: 'frontend-timeout-alert'
  params: {
    name: take('alert-${environmentName}-frontend-timeouts', 260)
    actions: [
      operationsActionGroup.outputs.resourceId
    ]
    alertDescription: 'Frontend requests timed out while waiting for a response.'
    criteria: {
      allof: [
        {
          criterionType: 'StaticThresholdCriterion'
          dimensions: []
          metricName: 'ResiliencyRequestTimeouts'
          metricNamespace: 'Microsoft.App/containerApps'
          name: 'FrontendRequestTimeouts'
          operator: 'GreaterThan'
          threshold: 0
          timeAggregation: 'Total'
        }
      ]
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
    }
    evaluationFrequency: 'PT1M'
    scopes: [
      frontendApp.outputs.resourceId
    ]
    severity: 1
    windowSize: 'PT5M'
    tags: commonTags
  }
}

module inferenceRestartAlert 'br/public:avm/res/insights/metric-alert:0.4.0' = {
  name: 'inference-restart-alert'
  params: {
    name: take('alert-${environmentName}-inference-restarts', 260)
    actions: [
      operationsActionGroup.outputs.resourceId
    ]
    alertDescription: 'The Apertus inference container restarted unexpectedly.'
    criteria: {
      allof: [
        {
          criterionType: 'StaticThresholdCriterion'
          dimensions: []
          metricName: 'RestartCount'
          metricNamespace: 'Microsoft.App/containerApps'
          name: 'InferenceRestarts'
          operator: 'GreaterThan'
          threshold: 0
          timeAggregation: 'Total'
        }
      ]
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
    }
    evaluationFrequency: 'PT1M'
    scopes: [
      inferenceApp.outputs.resourceId
    ]
    severity: 1
    windowSize: 'PT5M'
    tags: commonTags
  }
}

output AZURE_LOCATION string = location
output AZURE_CONTAINER_REGISTRY_NAME string = registry.outputs.name
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = registry.outputs.loginServer
output AZURE_KEY_VAULT_NAME string = keyVault.outputs.name
output AZURE_STORAGE_ACCOUNT_NAME string = storage.outputs.name
output AZURE_CONTAINER_APPS_ENVIRONMENT_NAME string = managedEnvironment.outputs.name
output FRONTEND_IDENTITY_RESOURCE_ID string = frontendIdentity.outputs.resourceId
output FRONTEND_IDENTITY_PRINCIPAL_ID string = frontendIdentity.outputs.principalId
output INFERENCE_IDENTITY_RESOURCE_ID string = inferenceIdentity.outputs.resourceId
output INFERENCE_IDENTITY_PRINCIPAL_ID string = inferenceIdentity.outputs.principalId
output SERVICE_FRONTEND_NAME string = frontendApp.outputs.name
output SERVICE_FRONTEND_URI string = 'https://${frontendApp.outputs.fqdn}'
output SERVICE_INFERENCE_NAME string = inferenceApp.outputs.name
output SERVICE_INFERENCE_FQDN string = inferenceApp.outputs.fqdn
output FOUNDRY_PROJECT_ENDPOINT string = 'https://${foundryAccountName}.services.ai.azure.com/api/projects/${foundryProjectName}'
output CONTENT_SAFETY_ENDPOINT string = contentSafety.outputs.endpoint
output APPLICATIONINSIGHTS_CONNECTION_STRING string = applicationInsights.outputs.connectionString