// Instagram MCP Server — Azure Container Apps deployment.
//
// Provisions everything needed to run the MCP server remotely with Entra ID auth:
//   - User-assigned managed identity (Key Vault read)
//   - Key Vault holding the Instagram/Facebook secrets (never in the image)
//   - The Container App itself (HTTPS ingress on 8000, image from ghcr.io,
//     KV-referenced secrets), joined to an EXISTING Container Apps environment
//
// The image is hosted on ghcr.io; the Container Apps environment (incl. its
// logging) is reused, not created here.
//
// Deploy:  az deployment group create -g <rg> -f infra/main.bicep -p infra/main.bicepparam
// Scope:   resource group

targetScope = 'resourceGroup'

@description('Base name for resources (lowercase alphanumerics).')
param baseName string = 'igmcp'

@description('Azure region for all resources. Must match the Container Apps environment region.')
param location string = resourceGroup().location

@description('Name of the existing Container Apps environment to deploy into.')
param existingEnvName string = 'env-takumi'

@description('Resource group of the existing Container Apps environment.')
param existingEnvResourceGroup string = 'Takumi'

@description('Container image to deploy, e.g. ghcr.io/artisanojp/ig-mcp:v1.')
param containerImage string

@description('Container registry host for private pulls (e.g. ghcr.io). Leave blank for a public image.')
param registryServer string = 'ghcr.io'

@description('Registry username (GitHub owner/user for ghcr.io). Leave blank for a public image.')
param registryUsername string = ''

@description('Registry password / token (GitHub PAT with read:packages). Leave blank for a public image.')
@secure()
param registryPassword string = ''

@description('Minimum replicas. 1 = one replica always warm (no cold starts, no dropped MCP sessions). 0 = scale-to-zero (cheapest, but cold start after idle).')
@minValue(0)
param minReplicas int = 1

@description('Maximum replicas. Sticky sessions are enabled so scaling out preserves MCP sessions.')
param maxReplicas int = 1

@description('vCPU per replica (ACA consumption smallest is 0.25).')
param cpu string = '0.25'

@description('Memory per replica (must pair with cpu; 0.25 vCPU -> 0.5Gi).')
param memory string = '0.5Gi'

// --- Entra ID (Microsoft 365) auth configuration (non-secret) ---
@description('Entra ID tenant (directory) ID.')
param entraTenantId string

@description('Entra ID app registration (client) ID for this resource server.')
param entraClientId string

@description('App role a caller must hold to use the server.')
param entraRequiredRole string = 'IGMCP.Use'

@description('Entra app client secret for the OAuth broker back-channel (server-side only).')
@secure()
param entraClientSecret string

@description('Public HTTPS base URL of the server (OAuth resource identifier). Leave blank to use the default Container Apps FQDN.')
param serverPublicUrl string = ''

@description('Custom domain hostname (e.g. igmcp.artisano.jp). Leave blank to skip.')
param customDomainName string = ''

@description('Resource ID of the managed certificate to bind to the custom domain. Required when customDomainName is set.')
param customDomainCertificateId string = ''

// --- Instagram / Facebook secrets (seeded into Key Vault) ---
@secure()
param instagramAccessToken string
@secure()
param facebookAppId string
@secure()
param facebookAppSecret string
@secure()
param instagramBusinessAccountId string = ''

var keyVaultName = take('${baseName}kv${uniqueString(resourceGroup().id)}', 24)
var identityName = '${baseName}-id'
var appName = '${baseName}-app'

// Built-in role definition IDs
var kvSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

// Private-registry pull is configured only when a password/token is supplied.
var usePrivateRegistry = !empty(registryPassword)
var registries = usePrivateRegistry ? [
  {
    server: registryServer
    username: registryUsername
    passwordSecretRef: 'registry-password'
  }
] : []
var registrySecret = usePrivateRegistry ? [
  {
    name: 'registry-password'
    value: registryPassword
  }
] : []
var kvSecrets = [
  {
    name: 'instagram-access-token'
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/instagram-access-token'
    identity: identity.id
  }
  {
    name: 'facebook-app-id'
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/facebook-app-id'
    identity: identity.id
  }
  {
    name: 'facebook-app-secret'
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/facebook-app-secret'
    identity: identity.id
  }
  {
    name: 'instagram-business-account-id'
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/instagram-business-account-id'
    identity: identity.id
  }
  {
    name: 'entra-client-secret'
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/entra-client-secret'
    identity: identity.id
  }
]

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
  }
}

resource kvSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, identity.id, kvSecretsUserRoleId)
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvSecretsUserRoleId)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource secretAccessToken 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'instagram-access-token'
  properties: {
    value: instagramAccessToken
  }
}

resource secretAppId 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'facebook-app-id'
  properties: {
    value: facebookAppId
  }
}

resource secretAppSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'facebook-app-secret'
  properties: {
    value: facebookAppSecret
  }
}

resource secretBusinessAccount 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'instagram-business-account-id'
  properties: {
    value: empty(instagramBusinessAccountId) ? 'unset' : instagramBusinessAccountId
  }
}

resource secretEntraClientSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'entra-client-secret'
  properties: {
    value: entraClientSecret
  }
}

// Reuse an existing Container Apps environment (it already has its own logging).
// The app's region is taken from this environment, so `location` must match it.
resource managedEnv 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: existingEnvName
  scope: resourceGroup(existingEnvResourceGroup)
}

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: managedEnv.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
        // Preserve MCP Streamable HTTP sessions if the app scales beyond one replica.
        stickySessions: {
          affinity: 'sticky'
        }
        // Custom domain + managed-cert binding (kept here so redeploys don't drop it).
        customDomains: empty(customDomainName) ? [] : [
          {
            name: customDomainName
            bindingType: 'SniEnabled'
            certificateId: customDomainCertificateId
          }
        ]
      }
      // ghcr.io pull credentials (omitted when the image is public).
      registries: registries
      // Instagram/Facebook secrets from Key Vault (managed identity) + optional
      // registry token. Secrets are never baked into the image.
      secrets: concat(registrySecret, kvSecrets)
    }
    template: {
      containers: [
        {
          name: 'instagram-mcp'
          image: containerImage
          resources: {
            cpu: json(cpu)
            memory: memory
          }
          env: [
            { name: 'MCP_TRANSPORT', value: 'streamable-http' }
            { name: 'MCP_HOST', value: '0.0.0.0' }
            { name: 'MCP_PORT', value: '8000' }
            { name: 'AUTH_ENABLED', value: 'true' }
            { name: 'ENTRA_TENANT_ID', value: entraTenantId }
            { name: 'ENTRA_CLIENT_ID', value: entraClientId }
            { name: 'ENTRA_REQUIRED_ROLE', value: entraRequiredRole }
            {
              name: 'SERVER_PUBLIC_URL'
              value: empty(serverPublicUrl) ? 'https://${appName}.${managedEnv.properties.defaultDomain}' : serverPublicUrl
            }
            { name: 'INSTAGRAM_ACCESS_TOKEN', secretRef: 'instagram-access-token' }
            { name: 'FACEBOOK_APP_ID', secretRef: 'facebook-app-id' }
            { name: 'FACEBOOK_APP_SECRET', secretRef: 'facebook-app-secret' }
            { name: 'INSTAGRAM_BUSINESS_ACCOUNT_ID', secretRef: 'instagram-business-account-id' }
            { name: 'ENTRA_CLIENT_SECRET', secretRef: 'entra-client-secret' }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/health'
                port: 8000
              }
              initialDelaySeconds: 15
              periodSeconds: 30
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/health'
                port: 8000
              }
              initialDelaySeconds: 10
              periodSeconds: 15
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
      }
    }
  }
  dependsOn: [
    kvSecretsUser
    secretAccessToken
    secretAppId
    secretAppSecret
    secretBusinessAccount
    secretEntraClientSecret
  ]
}

output keyVaultName string = keyVault.name
output identityClientId string = identity.properties.clientId
output containerAppName string = app.name
output containerAppFqdn string = app.properties.configuration.ingress.fqdn
