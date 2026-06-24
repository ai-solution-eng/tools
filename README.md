# PCAI tools repository

This repo contains some useful tools specific to PCAI that can help during hosted trials. 
Use the info below on how to push container images and take the **s3-browser** structure as a reference when adding your tools.

## Creating a token

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

## Authenticating

To authenticate just issue:

```
echo "YOUR_PERSONAL_ACCESS_TOKEN" | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin
```

Podman can be used instead of Docker keeping the same syntax.

## Building the image

A container can be built using Docker or Podman. Here is an example:

```
podman build --tag ghcr.io/ai-solution-eng/s3-browser:1.0.0 .
podman push ghcr.io/ai-solution-eng/s3-browser:1.0.0
```

## Enabling public access

The first time a container is pushed to GitHub, it's package has a **private** visibility and it must be
changed to **public** to allow a PCAI unit to download it. To do this:

- Click the "Packages" tab at organization level. The list of packages will be shown
- Click the package just uploaded (for example, the "s3-browser"). You will see the latest version
- Click the "Package settings" on the right side of the panel
- Scroll down to the "Danger Zone" and click "Change package visibility"
