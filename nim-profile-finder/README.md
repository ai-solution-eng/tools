# NIM Profile Finder

Extract NVIDIA NIM profile IDs and manifest details from NVIDIA NIM container images hosted on NGC without downloading the full image.

## Features

- Extracts NIM profile UUIDs and associated metadata:
  - Model name
  - GPU type
  - Backend engine
  - Precision
  - Tensor Parallelism (TP)
  - Pipeline Parallelism (PP)
  - Additional profile attributes
- Supports multiple NVIDIA NIM manifest formats:
  - Manifests exposed directly as image labels
  - Manifests stored within image layers
- For layer-based manifests, downloads only the required manifest layer, minimizing download time and bandwidth consumption.

## Prerequisites

### NGC Authentication

Authenticate to NGC before running the script:

```bash
docker login nvcr.io --username '$oauthtoken'
# Password: <your NGC API token>
```

### Required Tools

The following utilities must be installed:

- Docker
- skopeo
- jq

Verify installation:

```bash
docker --version
skopeo --version
jq --version
```

## Usage

```bash
./nim-profile-finder.sh \
  nvcr.io/nim/nvidia/nemotron-3-ultra-550b-a55b:latest
```

## What the Script Does

The script:

1. Retrieves the image manifest from NGC.
2. Determines where NIM profile information is stored.
3. Extracts profile metadata either from image labels or manifest layers.
4. Downloads only the layer containing the NIM manifest when necessary.
5. Displays available profile IDs and associated deployment attributes.

## Example Output

```text
Profile UUID: 7df754f6-a2d4-4b70-9a8c-xxxxxxxxxxxx
Model: Nemotron-3 Ultra 550B
GPU: H100
Backend: TensorRT-LLM
Precision: FP8
TP: 8
PP: 1
```

## Background

This utility automates the process described in the HPE Tech Hours article:

**Extracting NIM Model Profile IDs and Manifest Details from NVIDIA NIM Images Without Full Downloads**

The objective is to inspect NVIDIA NIM images and identify available deployment profiles without pulling potentially hundreds of gigabytes of model artifacts.

## Repository Structure

```text
nim-profile-finder/
├── README.md
└── nim-profile-finder.sh.sh
```

## Notes

- Requires access to the target NIM image in NGC.
- Some NIM images store metadata in image labels, while others store it in a dedicated layer.
- No full container image download is required.
``
