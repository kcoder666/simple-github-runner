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
# Fail loudly if gh errors or returns an empty token — otherwise config.sh would
# run with an empty --token and report a misleading "Bad credentials" (401).
fetch_token() {
    local kind="$1" token
    if ! token="$(gh api -X POST "${API_SCOPE}/actions/runners/${kind}" -q .token)" || [[ -z "${token}" ]]; then
        echo "ERROR: could not mint ${kind} for scope '${API_SCOPE}'." >&2
        echo "       Check GITHUB_PAT permissions (org runners need 'manage_runners:org'" >&2
        echo "       / 'Self-hosted runners: Read and write'; repo runners need repo Admin)" >&2
        echo "       and SSO authorization if the org enforces SAML." >&2
        return 1
    fi
    printf '%s' "${token}"
}

echo "Configuring GitHub Actions Runner (scope: ${API_SCOPE})..."
# Assign first so `set -e` aborts on a failed token fetch (a failure inside
# config.sh's argument substitution would not trigger set -e on its own).
reg_token="$(fetch_token registration-token)"
./config.sh --url "${REPO_URL}" --token "${reg_token}" --name "$(hostname)" --work "_work" --replace --ephemeral

cleanup() {
    echo "Removing runner..."
    local rm_token
    rm_token="$(fetch_token remove-token)" && ./config.sh remove --token "${rm_token}" || true
}

# Trap termination signals to clean up the runner from the GitHub UI.
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

# Start listening for jobs.
./run.sh & wait $!
