#!/bin/bash
set -euo pipefail

# Rebuild the runner image from the current Dockerfile.
#
# Usage:
#   ./rebuild.sh                 # build custom-github-runner:latest
#   ./rebuild.sh 2.336.0         # override the pinned RUNNER_VERSION
#   IMAGE=my-runner:dev ./rebuild.sh
#
# After rebuilding, recreate running runners to pick up the new image:
#   docker compose --profile repos up -d --force-recreate --scale repo-1=10

cd "$(dirname "$0")"

IMAGE="${IMAGE:-custom-github-runner:latest}"

# Optional first arg overrides the RUNNER_VERSION build arg.
build_args=()
if [[ $# -ge 1 ]]; then
    build_args+=(--build-arg "RUNNER_VERSION=$1")
fi

echo "Building ${IMAGE}..."
# ${arr[@]+"${arr[@]}"} expands safely even when empty under `set -u` (bash 3.2).
docker build ${build_args[@]+"${build_args[@]}"} -t "${IMAGE}" .

echo "Done: ${IMAGE}"
docker run --rm --entrypoint docker "${IMAGE}" --version
