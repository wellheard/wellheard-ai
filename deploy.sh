#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
# WellHeard AI — Fly.io Deployment Script
# Run from project root: bash deploy.sh
#
# Secrets are read from config/.env (not committed to git).
# Copy config/.env.example to config/.env and fill in your keys.
# ═══════════════════════════════════════════════════════════════════
set -e

echo "═══════════════════════════════════════════"
echo "  WellHeard AI — Fly.io Deploy"
echo "═══════════════════════════════════════════"

# Check that .env exists
ENV_FILE="config/.env"
if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: $ENV_FILE not found."
    echo "Copy config/.env.example to config/.env and fill in your API keys."
    exit 1
fi

# Check flyctl is installed
if ! command -v flyctl &> /dev/null; then
    echo "Installing flyctl..."
    curl -L https://fly.io/install.sh | sh
    export PATH="$HOME/.fly/bin:$PATH"
fi

# Check authentication
echo "Checking Fly.io authentication..."
if ! flyctl auth whoami &> /dev/null; then
    echo "Not logged in. Opening browser for login..."
    flyctl auth login
fi

# Create the app if it doesn't exist
echo "Creating app (if needed)..."
flyctl apps create wellheard-ai --org personal 2>/dev/null || echo "App already exists"

# Set all secrets from .env file
echo "Setting secrets from $ENV_FILE..."
flyctl secrets set $(grep -v "^#" "$ENV_FILE" | grep -v "^$" | tr '\n' ' ') --stage

# Deploy
echo "Deploying..."
flyctl deploy --ha=false

echo ""
echo "═══════════════════════════════════════════"
echo "  Deploy complete!"
echo ""
echo "  App URL:  https://wellheard-ai.fly.dev"
echo "  Health:   https://wellheard-ai.fly.dev/v1/health"
echo "  API Docs: https://wellheard-ai.fly.dev/docs"
echo "═══════════════════════════════════════════"
