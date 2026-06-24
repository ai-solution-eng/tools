# Login to podman

podman login https://harbor.ai-application.pcai0104.ld7.hpecolo.net/

# Build the image locally

podman build --tag harbor.ai-application.pcai0104.ld7.hpecolo.net/tools/s3-browser:1.0.0 .

# Push the image to Harbor

podman push harbor.ai-application.pcai0104.ld7.hpecolo.net/tools/s3-browser:1.0.0
