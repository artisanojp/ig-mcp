# Azure deployment — Instagram MCP Server

Runs the server on **Azure Container Apps** behind HTTPS, with **Microsoft Entra ID
(Microsoft 365)** authentication. The image is hosted on **GitHub Container Registry
(ghcr.io)**; the shared Instagram credentials live in **Azure Key Vault** and are injected
as environment variables via a managed identity — never baked into the image or committed
to the repo.

```
Claude (MCP client)
   │  OAuth 2.1 (Entra ID login, M365 creds)
   ▼
Container Apps ingress (HTTPS)        ◄── pulls image from ghcr.io (PAT)
   ▼
instagram-mcp container  ──validates JWT (app role IGMCP.Use)──►  tools
   │  managed identity
   ▼
Key Vault (INSTAGRAM_ACCESS_TOKEN, FACEBOOK_APP_*)  →  Instagram Graph API
```

## One-time setup

### 1. Entra ID app registrations

**Server (resource) app** — represents the MCP server:
- Application ID URI: `api://<server-client-id>`.
- Define an **app role** `IGMCP.Use` (member type: Users/Groups).
- Assign the role to a **security group** of allowed staff (Enterprise app → Users and groups).
- This `client-id` and the tenant id feed `ENTRA_CLIENT_ID` / `ENTRA_TENANT_ID`.

**Client app** — for Claude:
- A separate app registration whose `client-id` users enter when adding the connector.
- Add the delegated permission / consent to the server app's scope.
- (Entra ID has no public Dynamic Client Registration, so the client id is pre-shared.)

### 2. GitHub Container Registry access

- **Locally:** create a GitHub **PAT (classic) with `write:packages` + `read:packages`**,
  export it as `GHCR_PAT`. `deploy.sh` uses it to push the image, and the same token is
  stored as the Container App's registry pull credential.
- **In CI:** the `deploy` workflow pushes with the built-in `GITHUB_TOKEN` (`packages: write`)
  — no PAT needed there. The Container App still pulls with the PAT supplied at deploy time,
  so that PAT must retain `read:packages` on the `ig-mcp` package.

### 3. GitHub OIDC for CI Azure login

Create a federated credential so the CI `deploy` job authenticates to Azure without a stored secret:

```bash
az ad app create --display-name "github-igmcp-deploy"
# add a federated credential for subject repo:artisanojp/ig-mcp:ref:refs/heads/main
# assign it Contributor on the resource group (for `az containerapp update`)
```

Set GitHub repo **secrets**: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`.
Set GitHub repo **variables**: `ACA_APP_NAME` (e.g. `igmcp-app`), `ACA_RESOURCE_GROUP`.

## Provision

`deploy.sh` auto-loads `.env` (Instagram + Entra config) and pulls a GitHub token from
the `gh` CLI, so it's a single command:

```bash
gh auth refresh --scopes write:packages,read:packages   # one-time, grants package scope
./infra/deploy.sh
```

Overrides via env if needed: `RG`, `LOCATION` (default `japaneast`), `BASE_NAME`,
`GHCR_OWNER`, `IMAGE`, or an explicit `GHCR_PAT` (used instead of the gh token).

`deploy.sh` builds the image for `linux/amd64`, pushes it to ghcr.io, then provisions
everything and deploys the app on that image. To ship a new version:

```bash
docker build --platform linux/amd64 -t ghcr.io/artisanojp/ig-mcp:v2 . && docker push ghcr.io/artisanojp/ig-mcp:v2
az containerapp update -g Takumi -n igmcp-app --image ghcr.io/artisanojp/ig-mcp:v2
```

Subsequent pushes to `main` build, push, and roll out automatically via `.github/workflows/ci.yml`.

## Custom domain (optional)

1. `az containerapp hostname add` for `mcp.your-domain.com`.
2. Create the `CNAME` and `asuid.` `TXT` records at your DNS provider.
3. Bind a free managed certificate: `az containerapp hostname bind ... --validation-method CNAME`.
4. Set `serverPublicUrl` (or the `SERVER_PUBLIC_URL` env var) to `https://mcp.your-domain.com`
   so the OAuth protected-resource metadata advertises the right resource identifier.

## Notes

- **Cost defaults:** image hosted on **ghcr.io** (free, no ACR), reuses the existing
  `env-takumi` Container Apps environment (no new environment or Log Analytics),
  `minReplicas: 1` (one always-warm replica — no cold starts, no dropped MCP sessions),
  0.25 vCPU / 0.5 GiB. An always-on 0.25-vCPU replica runs ~a few dollars/month after the
  free grant; Key Vault costs pennies at this volume.
- **Cheaper option:** deploy with `-p minReplicas=0` for scale-to-zero (~$0 compute), at the
  cost of a cold start on the first request after ~5 min idle and dropped in-flight sessions.
- Sticky sessions are enabled, so raising `maxReplicas` preserves `Mcp-Session-Id` affinity.
- Health probes hit the unauthenticated `/health` route.
