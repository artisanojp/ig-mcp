using './main.bicep'

// Non-secret parameters. Fill in your Entra values.
param baseName = 'igmcp'

// Existing Container Apps environment to deploy into (the app joins this env).
param existingEnvName = 'env-takumi'
param existingEnvResourceGroup = 'Takumi'
param entraTenantId = '<your-entra-tenant-id>'
param entraClientId = '<your-server-app-registration-client-id>'
param entraRequiredRole = 'IGMCP.Use'
// Public URL clients use — must match the custom domain so OAuth metadata advertises it.
param serverPublicUrl = 'https://igmcp.artisano.jp'

// Custom domain + the managed certificate to bind (cert must be in Succeeded state).
param customDomainName = 'igmcp.artisano.jp'
param customDomainCertificateId = '/subscriptions/b7ca9fa9-9c92-499c-b52b-e7dbffed9292/resourceGroups/Takumi/providers/Microsoft.App/managedEnvironments/env-takumi/managedCertificates/igmcp.artisano.jp-env-taku-260601210148'

// Cost knobs (defaults: 1 always-warm replica, smallest compute).
param minReplicas = 1
param maxReplicas = 1
param cpu = '0.25'
param memory = '0.5Gi'

// Image + registry. deploy.sh overrides these via the CLI; the registry password
// is a GitHub PAT with read:packages and must NOT be hardcoded here.
param containerImage = readEnvironmentVariable('IMAGE', 'ghcr.io/artisanojp/ig-mcp:v1')
param registryServer = 'ghcr.io'
param registryUsername = readEnvironmentVariable('GHCR_USER', 'artisanojp')
param registryPassword = readEnvironmentVariable('GHCR_PAT', '')

// Secrets — passed at deploy time, never hardcoded.
param instagramAccessToken = readEnvironmentVariable('INSTAGRAM_ACCESS_TOKEN', '')
param facebookAppId = readEnvironmentVariable('FACEBOOK_APP_ID', '')
param facebookAppSecret = readEnvironmentVariable('FACEBOOK_APP_SECRET', '')
param instagramBusinessAccountId = readEnvironmentVariable('INSTAGRAM_BUSINESS_ACCOUNT_ID', '')
param entraClientSecret = readEnvironmentVariable('ENTRA_CLIENT_SECRET', '')
