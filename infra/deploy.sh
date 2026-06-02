#!/usr/bin/env bash
# Build + push the image to GitHub Container Registry (ghcr.io), then provision and
# deploy the Instagram MCP server to Azure Container Apps.
#
# Usage:  ./infra/deploy.sh        (run from anywhere; loads .env and gh token itself)
#
# Prerequisites:
#   - `az login` (Owner on the subscription, for Key Vault role assignment)
#   - Docker running locally
#   - `gh auth login` with package scope:
#         gh auth refresh --scopes write:packages,read:packages
#     (or export GHCR_PAT with a PAT that has write:packages,read:packages)
#   - The Entra app registrations from infra/README.md
# Instagram/Entra config and secrets come from .env (auto-sourced below) and are
# passed to the deployment — never stored in the repo.
set -euo pipefail

# Run from the repo root regardless of where this is invoked from.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Load .env (Instagram + Entra config) if present.
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

# Use an explicit GHCR_PAT if set, otherwise fall back to the gh CLI token.
GHCR_PAT="${GHCR_PAT:-$(gh auth token 2>/dev/null || true)}"

RG="${RG:-Takumi}"          # same RG as the env-takumi Container Apps environment
LOCATION="${LOCATION:-japaneast}"
BASE_NAME="${BASE_NAME:-igmcp}"

GHCR_OWNER="${GHCR_OWNER:-artisanojp}"
# Unique, traceable tag per deploy. A fixed tag (e.g. :v1) would not change the
# container template, so Azure Container Apps would NOT roll out a new revision.
TAG="${TAG:-$(git rev-parse --short HEAD 2>/dev/null || echo manual)-$(date +%Y%m%d%H%M%S)}"
IMAGE="${IMAGE:-ghcr.io/${GHCR_OWNER}/ig-mcp:${TAG}}"
GHCR_USER="${GHCR_USER:-$GHCR_OWNER}"

: "${GHCR_PAT:?no GitHub token. Run: gh auth refresh --scopes write:packages,read:packages (or export GHCR_PAT)}"
: "${ENTRA_TENANT_ID:?set ENTRA_TENANT_ID (add it to .env)}"
: "${ENTRA_CLIENT_ID:?set ENTRA_CLIENT_ID (add it to .env)}"
: "${ENTRA_CLIENT_SECRET:?set ENTRA_CLIENT_SECRET (broker back-channel secret; add it to .env)}"
: "${INSTAGRAM_ACCESS_TOKEN:?set INSTAGRAM_ACCESS_TOKEN (add it to .env)}"
: "${FACEBOOK_APP_ID:?set FACEBOOK_APP_ID (add it to .env)}"
: "${FACEBOOK_APP_SECRET:?set FACEBOOK_APP_SECRET (add it to .env)}"

echo "==> Building $IMAGE for linux/amd64 (Container Apps runs amd64)"
echo "$GHCR_PAT" | docker login ghcr.io -u "$GHCR_USER" --password-stdin
docker build --platform linux/amd64 -t "$IMAGE" .
docker push "$IMAGE"

echo "==> Ensuring resource group $RG ($LOCATION)"
az group create --name "$RG" --location "$LOCATION" --output none

echo "==> Deploying infrastructure (identity, Key Vault, ACA env + app)"
az deployment group create \
  --resource-group "$RG" \
  --template-file infra/main.bicep \
  --parameters infra/main.bicepparam \
  --parameters \
      baseName="$BASE_NAME" \
      location="$LOCATION" \
      containerImage="$IMAGE" \
      registryServer="ghcr.io" \
      registryUsername="$GHCR_USER" \
      registryPassword="$GHCR_PAT" \
      entraTenantId="$ENTRA_TENANT_ID" \
      entraClientId="$ENTRA_CLIENT_ID" \
      entraClientSecret="$ENTRA_CLIENT_SECRET" \
      instagramAccessToken="$INSTAGRAM_ACCESS_TOKEN" \
      facebookAppId="$FACEBOOK_APP_ID" \
      facebookAppSecret="$FACEBOOK_APP_SECRET" \
      instagramBusinessAccountId="${INSTAGRAM_BUSINESS_ACCOUNT_ID:-}" \
  --query properties.outputs --output json

echo
echo "==> Done. Test it:"
echo "    FQDN=\$(az containerapp show -g $RG -n ${BASE_NAME}-app --query properties.configuration.ingress.fqdn -o tsv)"
echo "    curl https://\$FQDN/health"
echo "    MCP_URL=https://\$FQDN/mcp TOKEN=<entra-token> ./scripts/mcp_call.sh call get_profile_info"
echo
echo "    To ship a new version: docker build --platform linux/amd64 -t $IMAGE . && docker push $IMAGE"
echo "    then: az containerapp update -g $RG -n ${BASE_NAME}-app --image $IMAGE"
