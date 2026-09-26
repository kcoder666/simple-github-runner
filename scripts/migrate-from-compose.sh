#!/bin/bash
set -euo pipefail

# Migrate from the compose-profile runners (org-runner / repo-N services) to the
# controller WITHOUT interrupting running jobs.
#
#   sudo ./scripts/migrate-from-compose.sh            # migrate, then wait for busy runners to drain
#   sudo ./scripts/migrate-from-compose.sh cleanup    # later: remove drained old runners
#
# Steps:
#   1. Record which old services run and how many replicas each has.
#   2. Build the new runner image, start the controller, create one target per
#      old service (max = old replica count, so capacity is unchanged).
#   3. Wait until the new runners are online.
#   4. Drain the old runners: disable their restart policy, stop the IDLE ones
#      now, and let BUSY ones finish their current job and exit on their own.
#      No running job is interrupted.
#   5. Restore the Docker socket group (the old start.sh chgrp-ed it on every
#      start) and remove old containers + their stale GitHub runner records as
#      they finish.
#
# Env overrides: MIN_IDLE (default 2), DRAIN_TIMEOUT seconds (default 10800),
#                KEEP_UNUSED_IMAGES=1 (default; don't prune unused images on a shared host)

cd "$(dirname "$0")/.."
PROJECT="${COMPOSE_PROJECT_NAME:-$(basename "$PWD")}"
MIN_IDLE="${MIN_IDLE:-2}"
DRAIN_TIMEOUT="${DRAIN_TIMEOUT:-10800}"
KEEP_UNUSED_IMAGES="${KEEP_UNUSED_IMAGES:-1}"
API="http://127.0.0.1:${DASHBOARD_PORT:-8080}"

log() { printf '\n==> %s\n' "$*"; }
env_get() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//'; }

[[ -f .env ]] || { echo "no .env here" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "cannot reach Docker — run with sudo" >&2; exit 1; }

old_containers() {
    docker ps -a --filter "label=com.docker.compose.project=${PROJECT}" \
        --format '{{.Names}} {{.Label "com.docker.compose.service"}}' |
        awk '$2 ~ /^(org-runner|repo-[0-9]+)$/ {print $1}'
}

api() {
    local method="$1" path="$2" body="${3:-}"
    curl -fsS -X "${method}" -H "Authorization: Bearer ${ADMIN_PASSWORD}" \
        -H 'Content-Type: application/json' ${body:+-d "${body}"} "${API}${path}"
}

is_busy() {
    # A runner executing a job has a Runner.Worker process.
    docker top "$1" -eo args 2>/dev/null | grep -q 'Runner.Worker'
}

# Delete offline runner records the old containers left behind (they were
# named after the container hostname: 12 hex chars).
delete_stale_records() {
    local pat url scope
    pat="$(env_get GITHUB_PAT)"
    [[ -n "${pat}" ]] || return 0
    for url in "$@"; do
        scope="${url#https://github.com/}"; scope="${scope%/}"
        if [[ "${scope}" == */* ]]; then scope="repos/${scope}"; else scope="orgs/${scope}"; fi
        # A PAT without org permissions gets 403 on org scopes; skip, don't abort.
        if ! listing="$(curl -fsS -H "Authorization: Bearer ${pat}" -H 'Accept: application/vnd.github+json' \
            "https://api.github.com/${scope}/actions/runners?per_page=100" 2>/dev/null)"; then
            echo "   ${scope}: cannot list runners with this credential — skipped"
            continue
        fi
        printf '%s' "${listing}" |
        python3 -c '
import json, re, sys
for r in json.load(sys.stdin)["runners"]:
    if r["status"] == "offline" and re.fullmatch(r"[0-9a-f]{12}", r["name"]):
        print(r["id"], r["name"])' |
        while read -r id name; do
            if curl -fsS -o /dev/null -X DELETE -H "Authorization: Bearer ${pat}" \
                "https://api.github.com/${scope}/actions/runners/${id}"; then
                echo "   deleted stale GitHub runner ${name} (${scope})"
            fi
        done
    done
}

# Removes exited old containers; prints how many are still running.
cleanup() {
    local remaining=0 c
    for c in $(old_containers); do
        if [[ "$(docker inspect -f '{{.State.Running}}' "${c}")" == "true" ]]; then
            remaining=$((remaining + 1))
        else
            docker rm -f "${c}" >/dev/null && echo "   removed ${c}" >&2
        fi
    done
    echo "${remaining}"
}

restore_socket_group() {
    if getent group docker >/dev/null; then
        chgrp docker /var/run/docker.sock && echo "   /var/run/docker.sock group -> docker"
    fi
}

urls=()
while IFS= read -r line; do urls+=("${line}"); done < <(grep -E '^(ORG_URL|REPO_URL_[0-9]+)=' .env | cut -d= -f2- | grep -v '^<' || true)

if [[ "${1:-}" == "cleanup" ]]; then
    log "Removing drained old runners"
    left="$(cleanup | tail -1)"
    restore_socket_group
    delete_stale_records ${urls[@]+"${urls[@]}"}
    echo "   ${left} old runner(s) still running a job"
    exit 0
fi

# --- 1. inventory ------------------------------------------------------------------
log "Old runners (project ${PROJECT})"
declare -a services=() counts=()
for svc in $(old_containers | xargs -r docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' | sort -u); do
    n="$(docker ps -a --filter "label=com.docker.compose.project=${PROJECT}" \
        --filter "label=com.docker.compose.service=${svc}" -q | wc -l | tr -d ' ')"
    services+=("${svc}"); counts+=("${n}")
    echo "   ${svc}: ${n} container(s)"
done

# --- 2. controller -------------------------------------------------------------------
ADMIN_PASSWORD="$(env_get ADMIN_PASSWORD)"
if [[ -z "${ADMIN_PASSWORD}" || "${ADMIN_PASSWORD}" == \<* ]]; then
    ADMIN_PASSWORD="$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 24)"
    printf '\n# Dashboard password (added by migrate-from-compose.sh)\nADMIN_PASSWORD=%s\n' "${ADMIN_PASSWORD}" >> .env
    echo "   generated ADMIN_PASSWORD (saved in .env)"
fi
chmod 600 .env

log "Building runner image"
latest="$(curl -fsS https://api.github.com/repos/actions/runner/releases/latest |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"].lstrip("v"))' || true)"
./rebuild.sh runner ${latest:+"${latest}"}

log "Starting controller"
SKIP_ENV_IMPORT=1 docker compose up -d --build controller
for _ in $(seq 1 60); do curl -fsS "${API}/healthz" >/dev/null 2>&1 && break; sleep 2; done
curl -fsS "${API}/healthz" >/dev/null || { echo "controller did not become healthy; see: docker compose logs controller" >&2; exit 1; }
if [[ "${KEEP_UNUSED_IMAGES}" == "1" ]]; then
    api PUT /api/settings '{"values":{"prune_unused_images_hours":0}}' >/dev/null
    echo "   shared host: pruning of unused images disabled (build cache/dangling only)"
fi

log "Creating targets"
existing="$(api GET /api/targets)"
for i in "${!services[@]}"; do
    svc="${services[$i]}"; n="${counts[$i]}"
    if [[ "${svc}" == "org-runner" ]]; then url="$(env_get ORG_URL)"; else url="$(env_get "REPO_URL_${svc#repo-}")"; fi
    [[ -n "${url}" ]] || { echo "   ${svc}: no URL in .env, skipping"; continue; }
    name="$(basename "${url%/}")"
    if python3 -c 'import json,sys; sys.exit(0 if any(t["url"].rstrip("/")==sys.argv[2].rstrip("/") for t in json.loads(sys.argv[1])) else 1)' "${existing}" "${url}"; then
        echo "   ${name}: already a target"; continue
    fi
    min=$(( MIN_IDLE < n ? MIN_IDLE : n ))
    api POST /api/targets "{\"name\":\"${name}\",\"url\":\"${url}\",\"min_idle\":${min},\"max_runners\":${n}}" >/dev/null
    echo "   ${name}: min idle ${min}, max ${n}  (${url})"
done

log "Waiting for new runners to come online"
for _ in $(seq 1 60); do
    idle="$(api GET /api/state | python3 -c 'import json,sys; print(sum(r["state"]=="idle" for r in json.load(sys.stdin)["runners"]))')"
    [[ "${idle}" -gt 0 ]] && break
    sleep 5
done
echo "   ${idle} new runner(s) idle"
[[ "${idle}" -gt 0 ]] || { echo "no new runner came online — old runners left untouched. Check the dashboard / controller logs." >&2; exit 1; }

# --- 3. drain -----------------------------------------------------------------------
log "Draining old runners (busy ones finish their job first)"
for c in $(old_containers); do
    docker update --restart=no "${c}" >/dev/null
    if [[ "$(docker inspect -f '{{.State.Running}}' "${c}")" != "true" ]]; then
        docker rm -f "${c}" >/dev/null; echo "   ${c}: not running — removed"
    elif is_busy "${c}"; then
        echo "   ${c}: busy — will exit after its current job"
    else
        docker stop -t 30 "${c}" >/dev/null; docker rm -f "${c}" >/dev/null; echo "   ${c}: idle — stopped"
    fi
done
# No old container can restart any more, so the socket stays fixed from here.
restore_socket_group

deadline=$(( $(date +%s) + DRAIN_TIMEOUT ))
while true; do
    left="$(cleanup | tail -1)"
    [[ "${left}" -eq 0 ]] && break
    if [[ $(date +%s) -ge ${deadline} ]]; then
        echo "   ${left} old runner(s) still busy after ${DRAIN_TIMEOUT}s. Re-run later with: sudo $0 cleanup"
        exit 0
    fi
    echo "   $(date +%H:%M:%S) ${left} old runner(s) still running a job…"
    sleep 30
done
delete_stale_records ${urls[@]+"${urls[@]}"}

log "Done. Old runners drained; the controller now owns all runners."
echo "   Dashboard: ssh -L 8080:127.0.0.1:${DASHBOARD_PORT:-8080} <host>, then http://localhost:8080"
