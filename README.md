# PCAI Tools Repository

This repo contains some useful tools specific to PCAI that can help during hosted trials. 
Use the info below on how to push container images and take the **s3-browser** structure as a reference when adding your tools.

## Tools

Below is a list of the available tools.

| Tool | Description |
| --- | --- |
| [chart-manager](chart-manager/README.md) | Helm chart deploying a Flask app on PCAI to add, delete, view, and date-filter charts, exposed via Istio VirtualService. |
| [model-downloader-cli](model-downloader-cli/README.md) | CLI tool that downloads AI models from HuggingFace or NVIDIA NGC to a local directory or S3 bucket, with resume support and progress bars. Useful in air-gapped hosted trials.|
| [model-downloader-web](model-downloader-web/README.md) | HTML frontend with support to run model downloads in parallel (4 models by default, 8 threads each). Also supports patching MLIS with some of our most commonly used models.|
| [s3-browser](s3-browser/README.md) | Web-based browser for navigating S3-compatible object storage.|
| [sql-handler](sql-handler/README.md) | Alternative to EZPresto for querying SQL in a few commonly deployed modes. Avoiding JDBC yields a 3-4x speed up with typical SQL queries, and avoids timeouts that are typical for larger datasets.|

## How to push container images to GitHub

### Creating a token

The first step is to create a personal access token (PAT):
- Click the user profile icon and then "Settings" from the dropdown menu to reach the user's profile page
- On the left menu, find "Developer settings" near the bottom and open it
- Click "Personal Access Tokens" on the left menu and then "Tokens (classic)"
- Click "Generate new token" and then "Generate new token (classic)"

In the page that opens, select:

- write:packages
- delete:packages

Add some notes because they cannot be empty and click "Generate token"
Save the token somewhere.

### Authenticating

To authenticate just issue:

```
echo "YOUR_PERSONAL_ACCESS_TOKEN" | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin
```

Podman can be used instead of Docker keeping the same syntax.

### Building the image

A container can be built using Docker or Podman. Here is an example:

```
podman build --tag ghcr.io/ai-solution-eng/s3-browser:1.0.0 .
podman push ghcr.io/ai-solution-eng/s3-browser:1.0.0
```

### Enabling public access

The first time a container is pushed to GitHub, it's package has a **private** visibility and it must be
changed to **public** to allow a PCAI unit to download it. To do this:

- Click the "Packages" tab at organization level. The list of packages will be shown
- Click the package just uploaded (for example, the "s3-browser"). You will see the latest version
- Click the "Package settings" on the right side of the panel
- Scroll down to the "Danger Zone" and click "Change package visibility"
