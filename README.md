# Simple GitHub Runner

[![Build](https://github.com/kcoder666/simple-github-runner/actions/workflows/docker-build.yml/badge.svg)](https://github.com/kcoder666/simple-github-runner/actions/workflows/docker-build.yml)

Self-hosted GitHub Actions runners that **look after themselves**. One small controller container manages ephemeral runner containers across many repos and orgs, heals them when they wedge, and gives you a web dashboard to run it all.

```
                      ┌──────────────────────── Docker host ─────────────────────────┐
  GitHub API  ◄──────►│  controller  (dashboard :8080, reconcile loop, housekeeping) │
  (JIT configs,       │      │ docker.sock                                           │
   runner status,     │      ├──► ghr-1-web-app-a1b2c3   (ephemeral: 1 job, exits)   │
   webhooks)          │      ├──► ghr-1-web-app-d4e5f6                               │
                      │      └──► ghr-2-acme-org-0718aa                              │
                      └──────────────────────────────────────────────────────────────┘
```

## Features

- **Many repos and orgs** — each *target* (a repo URL or an org URL) gets its own pool, labels, runner group, CPU/memory limits and Docker-socket toggle.
- **Warm pools + autoscaling** — keeps `min idle` runners ready, backfills as jobs start, caps at `max runners`. With a webhook, scales from zero and reacts instantly.
- **Just-in-time runners** — the controller mints single-use JIT configs; your PAT never enters a job container.
- **Self-healing** — replaces runners that never register, drop offline, hang on a job, crash, or get OOM-killed; cleans up orphaned GitHub records and containers; backs off exponentially from crash loops.
- **Runner auto-update** — watches `actions/runner` releases, rebuilds the runner image, and recycles idle runners onto it.
- **Disk hygiene** — scheduled pruning of build cache and container-action images, with an emergency prune when free space runs low.
- **Credential monitoring** — shows PAT expiry and alerts before it lapses; supports GitHub App auth, which never expires.
- **Dashboard** — targets, live runners (logs, recycle, kill), event history, settings, image and disk controls.
- **Alerts and observability** — Slack/Discord webhook alerts, `/healthz`, Prometheus `/metrics`, and a watchdog that restarts the controller if its loop stalls.

## Why runners got stuck every month (and what handles each)

| Failure | What you saw | What the controller does |
|---|---|---|
| **PAT expired** (fine-grained PATs expire after 30–90 days) | Every restart fails to mint a token; runners vanish | Shows the expiry on the dashboard and alerts 14 days ahead. Better: use a **GitHub App**, which has no expiry |
| **Runner version fell behind** (GitHub stops sending jobs to outdated runners) | Runners show online but jobs sit in *Queued* | Rebuilds the image when a release ships; recycles idle runners |
| **Disk filled up** (container-action images and build cache on the shared socket) | Jobs fail with `no space left on device`, or containers won't start | Daily prune, plus an aggressive prune and an alert below 15% free |
| **Listener hung** (network blip, dead session) | Container still "Up", GitHub shows the runner offline | Replaced after `offline_grace` (3 min) |
| **Registration never completed** | Container running, no runner in GitHub | Replaced after `registration_timeout` (5 min) |
| **Crash loop** from `restart: always` | Burning API rate limit, filling logs | Exponential backoff per target, with the last log lines in the event |
| **Job hung forever** | One runner permanently busy | Killed after `job_timeout` (12 h, configurable) |

## Quick start

```bash
cp .env.example .env     # set ADMIN_PASSWORD and GITHUB_PAT (or a GitHub App)
docker compose up -d --build
open http://localhost:8080
```

On first boot the controller builds the runner image with the latest runner release (a few minutes), then starts runners for your targets. Add targets under **Targets → Add target**:

| Field | Meaning |
|---|---|
| URL | `https://github.com/<owner>/<repo>` for a repo, or `https://github.com/<org>` for an org-wide pool |
| Extra labels | Added to `self-hosted, linux, x64/arm64`. A job runs here when its `runs-on` labels are a subset |
| Min idle | Runners kept waiting for jobs. `0` scales from zero, which needs the webhook |
| Max runners | Hard cap on concurrent runners |
| Runner group | Org targets only |
| CPU / memory | Per-runner container limits, e.g. `2` CPUs and `8g` |
| Docker socket | Lets jobs use container actions and `docker` commands (see [Security](#security)) |

Target workflows with:

```yaml
runs-on: [self-hosted, linux, gpu]   # any subset of the target's labels
```

## Credentials

Use one of these.

**GitHub App (recommended).** Create an App with the *Administration: Read & write* repository permission (for repo targets) and/or the *Self-hosted runners: Read & write* organization permission (for org targets), install it on each owner, then set:

```env
GITHUB_APP_ID=123456
GITHUB_APP_PRIVATE_KEY_FILE=/run/secrets/github-app.pem   # mount the key, or use GITHUB_APP_PRIVATE_KEY
```

**Personal Access Token.** Repo targets need a fine-grained PAT with *Administration: Read & write*. Org targets need a classic PAT with `manage_runners:org`. Set `GITHUB_PAT`. The dashboard shows when it expires.

## Webhook (optional, recommended)

Without a webhook the controller polls, keeping `min idle` runners warm and backfilling within one reconcile interval (20 s). With one, queued jobs trigger an immediate scale-up, and `min idle = 0` works.

1. Set `GITHUB_WEBHOOK_SECRET` in `.env` and restart.
2. On the repo or org, go to **Settings → Webhooks → Add webhook**. Set the payload URL to `https://<controller-host>/webhook/github`, the content type to `application/json`, the secret to the same value, and select only the **Workflow jobs** event.

The controller must be reachable from GitHub for this, for example via a reverse proxy or tunnel.

## Health and recovery settings

All of these can be changed live under **System → Settings**:

| Setting | Default | |
|---|---|---|
| `reconcile_interval` | 20 s | Loop period |
| `registration_timeout` | 5 min | Replace a runner that never comes online |
| `offline_grace` | 3 min | Replace a runner that went offline |
| `idle_max_age` | 6 h | Recycle long-lived idle runners |
| `job_timeout` | 12 h | Kill a runner busy for longer than this (0 = off) |
| `crash_threshold` / `crash_window` | 4 in 10 min | Trigger spawn backoff (1 min doubling up to 30 min) |
| `disk_min_free_pct` | 15 % | Emergency prune and alert threshold |
| `prune_interval_hours` | 24 | Scheduled prune |
| `prune_unused_images_hours` | 168 | Also prune unused images older than this |
| `auto_update_runner` | on | Rebuild on new `actions/runner` releases |
| `notify_webhook_url` | — | Slack or Discord incoming webhook for alerts |

> [!WARNING]
> Pruning acts on the **whole Docker host**, not just runner images: it removes build cache, dangling images, and (by default) images unused for 7 days. That is what you want on a dedicated runner host. If the host runs other workloads, raise `prune_unused_images_hours` or set it to `0`.

## Operations

```bash
docker compose logs -f controller       # controller log
curl localhost:8080/healthz             # 200 when the reconcile loop is alive
curl localhost:8080/metrics             # Prometheus metrics
curl -H "Authorization: Bearer $ADMIN_PASSWORD" localhost:8080/api/state   # full state as JSON
./rebuild.sh                            # after pulling changes: rebuild and restart the controller
./rebuild.sh runner 2.337.0             # force the runner image to a given version
```

State (targets, settings, events) lives in the `controller-data` volume. Runners are disposable: after a controller restart it adopts running containers and carries on.

### How it works

Each reconcile cycle, for every target, the controller:

1. Lists its runner containers (Docker labels) and runner records (GitHub API, by name prefix `ghr-<target id>-`).
2. Classifies each runner as `starting`, `idle`, `busy`, `offline`, `stale`, `stuck`, `exited`, `failed`, or `orphan`. The rules are in [`controller/app/planner.py`](controller/app/planner.py) and fully unit-tested.
3. Removes finished or broken runners, deregisters idle runners before removing them (GitHub refuses if a job was just assigned, so nothing gets cut off), and spawns JIT runners to restore the warm pool.

If GitHub's API is unreachable, health decisions pause rather than killing healthy runners.

### Migrating from the compose-profile setup

Migrate without interrupting running jobs:

```bash
git pull
sudo ./scripts/migrate-from-compose.sh
```

The script does the following:
1. Records the old `org-runner` / `repo-N` services and their replica counts.
2. Builds the new runner image and starts the controller.
3. Creates one target per service, with max = the old replica count and min idle 2.
4. Waits for the new runners to come online.
5. **Drains** the old runners. Idle ones stop now; busy ones finish their current job and exit.
6. Restores the Docker socket's group and deletes the old runners' stale GitHub records.

If jobs are still running after 3 hours, it stops waiting. Finish later with `sudo ./scripts/migrate-from-compose.sh cleanup`.

On a shared host it also turns off pruning of unused images; pass `KEEP_UNUSED_IMAGES=0` to keep it on.

### Standalone runner (no controller)

The runner image still works without the controller:

```bash
docker build -t custom-github-runner:latest runner
docker run -d --restart always -e REPO_URL=https://github.com/<owner>/<repo> -e GITHUB_PAT=<pat> custom-github-runner:latest
```

## Repository layout

| Path | Purpose |
|---|---|
| `runner/` | Runner image (`Dockerfile`, `start.sh`: JIT mode or standalone PAT mode) |
| `controller/app/` | Controller: `planner.py` (decisions), `controller.py` (loops), `github.py`, `docker_ops.py`, `main.py` (API), `static/` (dashboard) |
| `controller/tests/` | Unit tests (`cd controller && pip install -r requirements-dev.txt && pytest`) |
| `docker-compose.yml` | Runs the controller |

## Security

- **The Docker socket is root on the host.** The controller needs it. Runners get it only if their target has *Docker socket* enabled, and then any workflow on that target can control the host. Only enable it for trusted repos.
- **Keep the dashboard private.** It controls the Docker host, and it binds to `127.0.0.1` by default (use `ssh -L 8080:127.0.0.1:8080 <host>`). To expose it, set `DASHBOARD_BIND=0.0.0.0`, but only behind a VPN or an HTTPS reverse proxy, and set `COOKIE_SECURE=true` when served over HTTPS. If `ADMIN_PASSWORD` is unset, a random one is generated and printed to the controller log.
- **Credentials stay in the controller.** Job containers receive only a single-use JIT config, which is removed from the environment before any job runs.
- **Public repos are risky.** Forked PRs can run arbitrary code on self-hosted runners. Prefer private repos.
- The runner user has passwordless `sudo` inside its container. Ephemeral containers limit the blast radius between jobs.

## License

MIT
