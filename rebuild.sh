#!/bin/bash
set -euo pipefail

# Rebuild images from the current checkout.
#
# Usage:
#   ./rebuild.sh                 # rebuild + restart the controller (docker compose)
#   ./rebuild.sh runner          # rebuild the runner image only (latest pinned version)
#   ./rebuild.sh runner 2.337.0  # rebuild the runner image with a specific RUNNER_VERSION
#
# You rarely need the runner form: the controller builds the runner image on
# first boot and rebuilds it whenever actions/runner ships a release (and the
# dashboard has a "Rebuild now" button). Idle runners on the old image are
# recycled automatically.

cd "$(dirname "$0")"

if [[ "${1:-}" == "runner" ]]; then
    IMAGE="${RUNNER_IMAGE:-custom-github-runner:latest}"
    build_args=()
    if [[ $# -ge 2 ]]; then
        build_args+=(--build-arg "RUNNER_VERSION=$2")
    fi
    echo "Building ${IMAGE}..."
    # ${arr[@]+"${arr[@]}"} expands safely even when empty under `set -u` (bash 3.2).
    docker build ${build_args[@]+"${build_args[@]}"} --label ghr.managed_image=true -t "${IMAGE}" runner
    echo "Done: ${IMAGE} (runner $(docker inspect -f '{{index .Config.Labels "ghr.runner_version"}}' "${IMAGE}"))"
    exit 0
fi

docker compose up -d --build
echo "Controller restarted. Dashboard: http://localhost:${DASHBOARD_PORT:-8080}"
