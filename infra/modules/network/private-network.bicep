param location string
param name string
param tags object

var privateDnsZoneNames = [
  'privatelink.azurecr.io'
  'privatelink.cognitiveservices.azure.com'
  'privatelink.file.${environment().suffixes.storage}'
  'privatelink.vaultcore.azure.net'
]

resource virtualNetwork 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: name
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [
        '10.42.0.0/21'
      ]
    }
    subnets: [
      {
        name: 'snet-aca'
        properties: {
          addressPrefix: '10.42.0.0/23'
          delegations: [
            {
              name: 'Microsoft.App-environments'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
      {
        name: 'snet-private-endpoints'
        properties: {
          addressPrefix: '10.42.2.0/24'
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
  tags: tags
}

resource privateDnsZones 'Microsoft.Network/privateDnsZones@2024-06-01' = [for zoneName in privateDnsZoneNames: {
  name: zoneName
  location: 'global'
  tags: tags
}]

resource privateDnsZoneLinks 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = [for (zoneName, index) in privateDnsZoneNames: {
  parent: privateDnsZones[index]
  name: '${name}-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: virtualNetwork.id
    }
  }
  tags: tags
}]

output infrastructureSubnetResourceId string = resourceId('Microsoft.Network/virtualNetworks/subnets', name, 'snet-aca')
output privateEndpointSubnetResourceId string = resourceId('Microsoft.Network/virtualNetworks/subnets', name, 'snet-private-endpoints')
output privateDnsZoneResourceIds object = {
  acr: privateDnsZones[0].id
  cognitiveServices: privateDnsZones[1].id
  storageFile: privateDnsZones[2].id
  keyVault: privateDnsZones[3].id
}
output resourceId string = virtualNetwork.id
