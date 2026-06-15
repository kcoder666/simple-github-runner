#!/bin/bash
set -euo pipefail

# Required env:
#   REPO_URL    - repo or org URL, e.g. https://github.com/owner/repo or https://github.com/org
#   GITHUB_PAT  - PAT (or GitHub App token) with rights to manage self-hosted runners
#
# Ephemeral runners are one-shot: after a single job they deregister and exit.
# docker-compose `restart: always` then restarts the container, so we must mint a
# FRESH registration token on every (re)start — a static token is single-use and
# would 404 on the second registration.

export GH_TOKEN="${GITHUB_PAT}"

# Derive the GitHub API scope (org vs repo) from REPO_URL.
# https://github.com/owner/repo -> repos/owner/repo   (2 path segments)
# https://github.com/org        -> orgs/org           (1 path segment)
path="${REPO_URL#https://github.com/}"
path="${path%/}"
if [[ "${path}" == */* ]]; then
    API_SCOPE="repos/${path}"
else
    API_SCOPE="orgs/${path}"
fi

# Mint a short-lived token via the GitHub API (registration-token | remove-token).
fetch_token() {
    gh api -X POST "${API_SCOPE}/actions/runners/$1" -q .token
}

echo "Configuring GitHub Actions Runner (scope: ${API_SCOPE})..."
./config.sh --url "${REPO_URL}" --token "$(fetch_token registration-token)" --name "$(hostname)" --work "_work" --replace --ephemeral

cleanup() {
    echo "Removing runner..."
    ./config.sh remove --token "$(fetch_token remove-token)" || true
}

# Trap termination signals to clean up the runner from the GitHub UI.
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

# Start listening for jobs.
./run.sh & wait $!
