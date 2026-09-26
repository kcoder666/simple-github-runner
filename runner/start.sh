#!/bin/bash
set -euo pipefail

# Two ways to start this runner:
#
# 1. Controller mode (recommended) — the controller mints a single-use JIT
#    config via the GitHub API and passes it in:
#      RUNNER_JITCONFIG  - encoded JIT runner config
#    The PAT never enters the container, and the runner is ephemeral by
#    construction (one job, then it exits and GitHub forgets it).
#
# 2. Standalone mode — the container mints its own registration token:
#      REPO_URL    - repo or org URL, e.g. https://github.com/owner/repo or https://github.com/org
#      GITHUB_PAT  - PAT (or GitHub App token) with rights to manage self-hosted runners
#    Ephemeral runners are one-shot, so a FRESH registration token is minted on
#    every (re)start — a static token is single-use and would 404 the second time.

# Container actions (runs.using: "docker") need to reach a Docker daemon via the
# bind-mounted host socket. The controller adds the socket's group to the
# container (group_add), so this is normally a no-op. In standalone mode the
# socket is owned by root:<host-docker-gid>, which the unprivileged `docker` user
# can't use, so fall back to re-grouping it to the in-container `docker` group.
if [[ -S /var/run/docker.sock && ! -w /var/run/docker.sock ]]; then
    sudo chgrp docker /var/run/docker.sock 2>/dev/null || true
    sudo chmod g+rw /var/run/docker.sock 2>/dev/null || true
fi

if [[ -n "${RUNNER_JITCONFIG:-}" ]]; then
    # Keep the (single-use) config out of the environment jobs inherit.
    jitconfig="${RUNNER_JITCONFIG}"
    unset RUNNER_JITCONFIG
    echo "Starting JIT runner..."
    exec ./run.sh --jitconfig "${jitconfig}"
fi

: "${REPO_URL:?REPO_URL is required (or RUNNER_JITCONFIG in controller mode)}"
: "${GITHUB_PAT:?GITHUB_PAT is required (or RUNNER_JITCONFIG in controller mode)}"

# Keep the PAT as a plain shell variable, not an exported one, so workflow jobs
# (children of run.sh) cannot read it from their environment.
pat="${GITHUB_PAT}"
export -n GITHUB_PAT
unset GH_TOKEN

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
    if ! token="$(GH_TOKEN="${pat}" gh api -X POST "${API_SCOPE}/actions/runners/${kind}" -q .token)" || [[ -z "${token}" ]]; then
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
config_args=(--url "${REPO_URL}" --token "${reg_token}" --name "$(hostname)" --work "_work" --replace --ephemeral --unattended)
if [[ -n "${RUNNER_LABELS:-}" ]]; then
    config_args+=(--labels "${RUNNER_LABELS}")
fi
./config.sh "${config_args[@]}"

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
