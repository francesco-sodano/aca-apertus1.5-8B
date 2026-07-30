param name string
param location string
param environmentResourceId string
param identityResourceId string
param registryServer string
param image string
param keyVaultUri string
param modelEndpoint string
param contentSafetyEndpoint string
param foundryProjectEndpoint string
param foundryGroundingModel string
param appInsightsConnectionString string
param configureApplicationSecrets bool
param entraClientId string
param entraTenantId string
param tags object

resource frontendApp 'Microsoft.App/containerApps@2025-01-01' = {
  name: name
  location: location
  identity: {
    type: 'SystemAssigned, UserAssigned'
    userAssignedIdentities: {
      '${identityResourceId}': {}
    }
  }
  properties: {
    environmentId: environmentResourceId
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        allowInsecure: false
        external: configureApplicationSecrets
        stickySessions: {
          affinity: 'sticky'
        }
        targetPort: 8000
        transport: 'auto'
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      registries: [
        {
          identity: identityResourceId
          server: registryServer
        }
      ]
      secrets: configureApplicationSecrets ? [
        {
          identity: identityResourceId
          keyVaultUrl: '${keyVaultUri}secrets/vllm-api-key'
          name: 'vllm-api-key'
        }
        {
          identity: identityResourceId
          keyVaultUrl: '${keyVaultUri}secrets/model-health-token'
          name: 'model-health-token'
        }
        {
          identity: identityResourceId
          keyVaultUrl: '${keyVaultUri}secrets/entra-client-secret'
          name: 'entra-client-secret'
        }
      ] : []
    }
    template: {
      containers: [
        {
          name: 'frontend'
          image: image
          env: concat([
            {
              name: 'MODEL_ENDPOINT'
              value: modelEndpoint
            }
            {
              name: 'MODEL_ID'
              value: 'swiss-ai/Apertus-v1.5-8B'
            }
            {
              name: 'CONTENT_SAFETY_ENDPOINT'
              value: contentSafetyEndpoint
            }
            {
              name: 'FOUNDRY_PROJECT_ENDPOINT'
              value: foundryProjectEndpoint
            }
            {
              name: 'FOUNDRY_GROUNDING_MODEL'
              value: foundryGroundingModel
            }
            {
              name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
              value: appInsightsConnectionString
            }
            {
              name: 'OTEL_SERVICE_NAME'
              value: 'apertus-frontend'
            }
            {
              name: 'MAX_CONCURRENT_REQUESTS'
              value: '4'
            }
            {
              name: 'REQUESTS_PER_MINUTE'
              value: '6'
            }
            {
              name: 'ADMISSION_QUEUE_TIMEOUT_SECONDS'
              value: '5'
            }
          ], configureApplicationSecrets ? [
            {
              name: 'VLLM_API_KEY'
              secretRef: 'vllm-api-key'
            }
            {
              name: 'MODEL_HEALTH_TOKEN'
              secretRef: 'model-health-token'
            }
          ] : [])
          probes: [
            {
              type: 'Startup'
              httpGet: {
                path: '/healthz'
                port: 8000
                scheme: 'HTTP'
              }
              failureThreshold: 12
              periodSeconds: 5
              timeoutSeconds: 3
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/healthz/ready'
                port: 8000
                scheme: 'HTTP'
              }
              failureThreshold: 3
              periodSeconds: 10
              timeoutSeconds: 3
            }
            {
              type: 'Liveness'
              httpGet: {
                path: '/healthz'
                port: 8000
                scheme: 'HTTP'
              }
              failureThreshold: 3
              periodSeconds: 30
              timeoutSeconds: 3
            }
          ]
          resources: {
            cpu: json('1')
            memory: '2Gi'
          }
        }
      ]
      scale: {
        minReplicas: configureApplicationSecrets ? 1 : 0
        maxReplicas: 3
        rules: [
          {
            name: 'http-concurrency'
            http: {
              metadata: {
                concurrentRequests: '4'
              }
            }
          }
        ]
      }
      terminationGracePeriodSeconds: 30
    }
  }
  tags: union(tags, {
    'azd-service-name': 'frontend'
  })
}

resource authConfig 'Microsoft.App/containerApps/authConfigs@2025-01-01' = if (configureApplicationSecrets) {
  parent: frontendApp
  name: 'current'
  properties: {
    globalValidation: {
      excludedPaths: [
        '/healthz'
        '/healthz/ready'
        '/healthz/model'
      ]
      redirectToProvider: 'azureActiveDirectory'
      unauthenticatedClientAction: 'RedirectToLoginPage'
    }
    httpSettings: {
      requireHttps: true
    }
    identityProviders: {
      azureActiveDirectory: {
        registration: {
          clientId: entraClientId
          clientSecretSettingName: 'entra-client-secret'
          openIdIssuer: '${environment().authentication.loginEndpoint}${entraTenantId}/v2.0'
        }
        validation: {
          allowedAudiences: [
            entraClientId
          ]
        }
      }
    }
    login: {
      tokenStore: {
        enabled: false
      }
    }
    platform: {
      enabled: true
      runtimeVersion: '~1'
    }
  }
}

output name string = frontendApp.name
output resourceId string = frontendApp.id
output fqdn string = frontendApp.properties.configuration.ingress.fqdn
output systemAssignedPrincipalId string = frontendApp.identity.principalId!