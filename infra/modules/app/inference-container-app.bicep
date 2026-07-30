param name string
param location string
param environmentResourceId string
param identityResourceId string
param registryServer string
param image string
param keyVaultUri string
param modelRevision string
param modelStorageName string
param configureApplicationSecrets bool
param tags object

resource inferenceApp 'Microsoft.App/containerApps@2025-01-01' = {
  name: name
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityResourceId}': {}
    }
  }
  properties: {
    environmentId: environmentResourceId
    workloadProfileName: 'gpu-a100'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        allowInsecure: false
        external: false
        targetPort: 8000
        transport: 'http'
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
          keyVaultUrl: '${keyVaultUri}secrets/hugging-face-token'
          name: 'hugging-face-token'
        }
        {
          identity: identityResourceId
          keyVaultUrl: '${keyVaultUri}secrets/vllm-api-key'
          name: 'vllm-api-key'
        }
      ] : []
    }
    template: {
      containers: [
        {
          name: 'inference'
          image: image
          env: concat([
            {
              name: 'MODEL_ID'
              value: 'swiss-ai/Apertus-v1.5-8B'
            }
            {
              name: 'MODEL_REVISION'
              value: modelRevision
            }
            {
              name: 'MAX_MODEL_LEN'
              value: '32768'
            }
            {
              name: 'GPU_MEMORY_UTILIZATION'
              value: '0.85'
            }
            {
              name: 'MAX_NUM_SEQS'
              value: '8'
            }
          ], configureApplicationSecrets ? [
            {
              name: 'HF_TOKEN'
              secretRef: 'hugging-face-token'
            }
            {
              name: 'VLLM_API_KEY'
              secretRef: 'vllm-api-key'
            }
          ] : [])
          probes: [
            {
              type: 'Startup'
              httpGet: {
                path: '/health'
                port: 8000
                scheme: 'HTTP'
              }
              failureThreshold: 90
              initialDelaySeconds: 5
              periodSeconds: 10
              timeoutSeconds: 5
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/health'
                port: 8000
                scheme: 'HTTP'
              }
              failureThreshold: 3
              periodSeconds: 10
              timeoutSeconds: 5
            }
            {
              type: 'Liveness'
              httpGet: {
                path: '/health'
                port: 8000
                scheme: 'HTTP'
              }
              failureThreshold: 3
              periodSeconds: 30
              timeoutSeconds: 5
            }
          ]
          resources: {
            cpu: json('24')
            memory: '220Gi'
          }
          volumeMounts: [
            {
              mountPath: '/home/appuser/hf-home'
              volumeName: 'model-cache'
            }
          ]
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 1
        cooldownPeriod: 1800
        pollingInterval: 30
        rules: [
          {
            name: 'http-concurrency'
            http: {
              metadata: {
                concurrentRequests: '1'
              }
            }
          }
        ]
      }
      terminationGracePeriodSeconds: 120
      volumes: [
        {
          name: 'model-cache'
          storageName: modelStorageName
          storageType: 'NfsAzureFile'
        }
      ]
    }
  }
  tags: tags
}

output name string = inferenceApp.name
output resourceId string = inferenceApp.id
output fqdn string = inferenceApp.properties.configuration.ingress.fqdn