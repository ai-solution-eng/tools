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
| [nim-profile-finder](nim-profile-finder/README.md) | Extracts NVIDIA manifest details from NIM container images hosted on NGC without downloading the full image. |
| [pcai-helm-port](pcai-helm-port/README.md) | Skill that ports Helm charts (or scaffolds new ones) so applications are deployable on PCAI. |
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

The first time a container is pushed to GitHub, it's package has a **private** visibility. Making it
**public** is one option to allow a PCAI unit to download it. To do this:

- Click the "Packages" tab at organization level. The list of packages will be shown
- Click the package just uploaded (for example, the "s3-browser"). You will see the latest version
- Click the "Package settings" on the right side of the panel
- Scroll down to the "Danger Zone" and click "Change package visibility"

> If you prefer to keep the package **private**, you can instead pull it into Kubernetes using a
> secret (imagePullSecret) — see the next section.

### Pulling a private image into Kubernetes with a Helm chart (alternative to public)

Keeping a package private and pulling it with a **Kubernetes imagePullSecret** is an alternative to
making it public. This is useful for internal images you don't want publicly visible.

1. Create the secret **once per namespace** that will run the workload. Use the helper script:

   ```
   ./create-ghcr-pull-secret.sh --username YOUR_GITHUB_USERNAME --password YOUR_PAT
   ./create-ghcr-pull-secret.sh --namespace my-ns --username YOUR_GITHUB_USERNAME --password YOUR_PAT
   ```

   The script creates a `docker-registry` secret (default name `ghcr-pull`) for `ghcr.io`
   (override `--server` and `--secret` for other registries). The PAT only needs the
   `read:packages` scope.

2. Reference the secret in your Helm chart values:

   ```yaml
   imagePullSecrets:
     - ghcr-pull
   ```

   Please ensure the Deployment attaches this `imagePullSecret`, so the kubelet uses those credentials when
   pulling the private image. Requirements:
   - the image reference in the chart points at the registry (e.g. `ghcr.io/<org>/<image>:<tag>`),
   - the secret exists in the **same namespace** as the Deployment, **before** the pod is created.
