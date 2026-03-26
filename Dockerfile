FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ curl && \
    rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies first (Docker cache layer)
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Copy source code
COPY . .

# .env is included via COPY . . above (for local/Fly deploys)
# Cloud Run uses --set-env-vars instead

# Expose API port (8000 for Fly, 8080 for Cloud Run — controlled via HV_PORT env var)
EXPOSE 8000 8080

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD curl -f http://localhost:${HV_PORT:-8000}/v1/health || exit 1

# Start server
CMD ["python", "main.py"]
