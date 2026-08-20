# Use Python 3.11 as base image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies, including Node.js for the Yjs helper used by Plane CE pages
RUN apt-get update && apt-get install -y \
    curl \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

# Install uv for faster package management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files and application code
COPY pyproject.toml ./
COPY uv.lock* ./
COPY plane_mcp/ ./plane_mcp/
COPY yjs-helper/ ./yjs-helper/

# Install Python package and the small Yjs helper dependencies
RUN uv pip install --system --no-cache . \
    && cd /app/yjs-helper \
    && npm install --omit=dev --no-audit --no-fund

# Expose port for HTTP transports (SSE, streamable-http, http)
EXPOSE 8211

# Set environment variables with defaults
ENV FASTMCP_PORT=8211

# Default to streamable-http transport, but allow override via command
ENTRYPOINT ["python", "-m", "plane_mcp"]
CMD ["http"]
