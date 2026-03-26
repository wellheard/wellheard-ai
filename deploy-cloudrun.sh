#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# WellHeard AI — Cloud Run Deployment Script
# Builds and deploys the voice AI platform to Google Cloud Run
#
# Usage:
#   ./deploy-cloudrun.sh                          # Deploy with defaults
#   ./deploy-cloudrun.sh --env .env.prod          # Deploy with custom env file
#   ./deploy-cloudrun.sh --dry-run                # Preview without deploying
#   ./deploy-cloudrun.sh --env .env.prod --dry-run
#
# Environment variables (from .env file or command-line):
#   GCP_PROJECT_ID          Google Cloud project ID (default: heyvox-491318)
#   GCP_REGION              Cloud Run region (default: us-east1)
#   ARTIFACT_REGISTRY_REPO  Artifact Registry repository (default: wellheard)
#   SERVICE_NAME            Cloud Run service name (default: wellheard-ai)
#   MIN_INSTANCES           Minimum instances (default: 1)
#   MAX_INSTANCES           Maximum instances (default: 100)
#   CPU_COUNT               CPU per instance (default: 1)
#   MEMORY_MB               Memory per instance in MB (default: 512)
#   TIMEOUT_SECONDS         Request timeout in seconds (default: 3600)
# ═══════════════════════════════════════════════════════════════════════════

set -e

# ── Color output for readability ──────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# ── Default configuration ─────────────────────────────────────────────────
GCP_PROJECT_ID="${GCP_PROJECT_ID:-heyvox-491318}"  # GCP project ID (immutable — cannot be renamed)
GCP_REGION="${GCP_REGION:-us-east1}"
ARTIFACT_REGISTRY_REPO="${ARTIFACT_REGISTRY_REPO:-wellheard}"
SERVICE_NAME="${SERVICE_NAME:-wellheard-ai}"
MIN_INSTANCES="${MIN_INSTANCES:-1}"
MAX_INSTANCES="${MAX_INSTANCES:-100}"
CPU_COUNT="${CPU_COUNT:-1}"
MEMORY_MB="${MEMORY_MB:-512}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-3600}"
DRY_RUN=false
ENV_FILE=""

# ── Parse command-line arguments ──────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case $1 in
        --env)
            ENV_FILE="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --help|-h)
            echo "Usage: $0 [options]"
            echo ""
            echo "Options:"
            echo "  --env FILE      Load environment variables from FILE"
            echo "  --dry-run       Preview deployment without applying changes"
            echo "  --help          Show this help message"
            exit 0
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            exit 1
            ;;
    esac
done

# ── Load environment file if provided ─────────────────────────────────────
if [ -n "$ENV_FILE" ]; then
    if [ ! -f "$ENV_FILE" ]; then
        echo -e "${RED}Error: Environment file not found: $ENV_FILE${NC}"
        exit 1
    fi
    echo -e "${BLUE}Loading environment from: $ENV_FILE${NC}"
    set -a
    source "$ENV_FILE"
    set +a
fi

# ── Validate required tools ───────────────────────────────────────────────
for tool in gcloud docker git; do
    if ! command -v "$tool" &> /dev/null; then
        echo -e "${RED}Error: Required tool not found: $tool${NC}"
        exit 1
    fi
done

# ── Display configuration ─────────────────────────────────────────────────
echo -e "${BLUE}╔════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║         WellHeard AI — Cloud Run Deployment                  ║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${BLUE}Configuration:${NC}"
echo "  Project ID:        $GCP_PROJECT_ID"
echo "  Region:            $GCP_REGION"
echo "  Service:           $SERVICE_NAME"
echo "  Artifact Registry: $GCP_REGION-docker.pkg.dev/$GCP_PROJECT_ID/$ARTIFACT_REGISTRY_REPO/$SERVICE_NAME"
echo "  Min Instances:     $MIN_INSTANCES"
echo "  Max Instances:     $MAX_INSTANCES"
echo "  CPU:               $CPU_COUNT"
echo "  Memory:            ${MEMORY_MB}Mi"
echo "  Timeout:           ${TIMEOUT_SECONDS}s"
echo ""

if [ "$DRY_RUN" = true ]; then
    echo -e "${YELLOW}DRY RUN MODE — No changes will be applied${NC}"
    echo ""
fi

# ── Set GCP project ───────────────────────────────────────────────────────
echo -e "${BLUE}[1/5] Setting GCP project...${NC}"
if [ "$DRY_RUN" = true ]; then
    echo "      gcloud config set project $GCP_PROJECT_ID"
else
    gcloud config set project "$GCP_PROJECT_ID" --quiet
    echo -e "${GREEN}✓ Project set${NC}"
fi
echo ""

# ── Enable required APIs ──────────────────────────────────────────────────
echo -e "${BLUE}[2/5] Enabling required APIs...${NC}"
APIS=("run.googleapis.com" "artifactregistry.googleapis.com" "cloudbuild.googleapis.com" "container.googleapis.com")
for api in "${APIS[@]}"; do
    if [ "$DRY_RUN" = true ]; then
        echo "      gcloud services enable $api"
    else
        gcloud services enable "$api" --quiet 2>/dev/null || true
        echo "      ✓ $api"
    fi
done
echo ""

# ── Create Artifact Registry repository if needed ──────────────────────────
echo -e "${BLUE}[3/5] Creating Artifact Registry repository...${NC}"
REGISTRY_URL="$GCP_REGION-docker.pkg.dev/$GCP_PROJECT_ID/$ARTIFACT_REGISTRY_REPO"

if [ "$DRY_RUN" = true ]; then
    echo "      gcloud artifacts repositories describe $ARTIFACT_REGISTRY_REPO --location=$GCP_REGION"
    echo "      (would create if not found)"
else
    if ! gcloud artifacts repositories describe "$ARTIFACT_REGISTRY_REPO" \
        --location="$GCP_REGION" &>/dev/null; then
        echo "      Creating repository: $ARTIFACT_REGISTRY_REPO"
        gcloud artifacts repositories create "$ARTIFACT_REGISTRY_REPO" \
            --location="$GCP_REGION" \
            --repository-format=docker \
            --quiet
        echo -e "${GREEN}✓ Repository created${NC}"
    else
        echo -e "${GREEN}✓ Repository already exists${NC}"
    fi
fi
echo ""

# ── Configure Docker authentication ────────────────────────────────────────
echo -e "${BLUE}[4/5] Configuring Docker authentication...${NC}"
if [ "$DRY_RUN" = true ]; then
    echo "      gcloud auth configure-docker $GCP_REGION-docker.pkg.dev"
else
    gcloud auth configure-docker "$GCP_REGION-docker.pkg.dev" --quiet
    echo -e "${GREEN}✓ Docker authentication configured${NC}"
fi
echo ""

# ── Submit build to Cloud Build ───────────────────────────────────────────
echo -e "${BLUE}[5/5] Submitting build to Cloud Build...${NC}"
echo ""

BUILD_CMD=(
    "gcloud"
    "builds"
    "submit"
    "--config=cloudbuild.yaml"
    "--project=$GCP_PROJECT_ID"
)

if [ "$DRY_RUN" = true ]; then
    echo -e "${YELLOW}DRY RUN:${NC}"
    echo "  ${BUILD_CMD[@]}"
    echo ""
    echo -e "${YELLOW}Cloud Run deployment would use:${NC}"
    echo "  Service:    $SERVICE_NAME"
    echo "  Image:      $REGISTRY_URL:latest"
    echo "  Region:     $GCP_REGION"
    echo "  Concurrency: 1"
    echo "  Min Instances: $MIN_INSTANCES"
    echo "  Max Instances: $MAX_INSTANCES"
    echo "  CPU: $CPU_COUNT"
    echo "  Memory: ${MEMORY_MB}Mi"
    echo "  Timeout: ${TIMEOUT_SECONDS}s"
else
    "${BUILD_CMD[@]}"
    echo ""
    echo -e "${GREEN}╔════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║                   Deployment Successful!                  ║${NC}"
    echo -e "${GREEN}╚════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "${BLUE}Service URL:${NC}"
    echo "  https://$SERVICE_NAME-$(gcloud projects describe $GCP_PROJECT_ID --format='value(projectNumber)' | cut -c1-5)xxxx-$(echo $GCP_REGION | tr '-' 'x').a.run.app"
    echo ""
    echo -e "${BLUE}Monitor deployment:${NC}"
    echo "  gcloud run services describe $SERVICE_NAME --region=$GCP_REGION"
    echo "  gcloud run services logs read $SERVICE_NAME --region=$GCP_REGION --limit=50"
fi

exit 0
