FROM python:3.12-slim

# System deps: ca-certificates for TLS, git for LFS, curl for debugging
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    git \
    git-lfs \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Initialize git-lfs (needed by huggingface_hub for some model files)
RUN git lfs install

# Python deps: huggingface_hub with hf_transfer for parallel downloads,
# hf_xet for large file support, requests for HTTP, boto3 for S3 uploads
RUN pip install --no-cache-dir \
    "huggingface_hub[hf_transfer]>=0.25.0" \
    "requests>=2.31.0" \
    "hf_transfer>=0.1.8" \
    "hf_xet>=1.1.0" \
    "boto3>=1.34.0"

# Default to hf_transfer enabled (can be overridden at runtime)
# Xet is NOT disabled — let huggingface_hub auto-detect large files
ENV HF_HUB_ENABLE_HF_TRANSFER=1

WORKDIR /workspace
ENTRYPOINT ["/bin/sh"]
