param location string
param name string
param privateDnsZoneResourceIds array
param service string
param subnetResourceId string
param targetResourceId string
param tags object

resource privateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: name
  location: location
  properties: {
    privateLinkServiceConnections: [
      {
        name: '${name}-connection'
        properties: {
          groupIds: [
            service
          ]
          privateLinkServiceId: targetResourceId
        }
      }
    ]
    subnet: {
      id: subnetResourceId
    }
  }
  tags: tags
}

resource privateDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: privateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [for (zoneResourceId, index) in privateDnsZoneResourceIds: {
      name: 'zone-${index}'
      properties: {
        privateDnsZoneId: zoneResourceId
      }
    }]
  }
}

output resourceId string = privateEndpoint.id
