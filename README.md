# Simple GitHub Runner

A minimal, containerized [self-hosted GitHub Actions runner](https://docs.github.com/en/actions/hosting-your-own-runners). Build the image once, then spin up one or many **ephemeral** runners that register on startup and clean themselves out of your GitHub UI on shutdown.

## Features

- Ubuntu 24.04 base, non-root `docker` user with passwordless `sudo`
- Pinned runner version (`2.335.1`) — override at build time
- **Ephemeral** runners: each runner accepts one job, then deregisters (clean, stateless CI)
- Graceful cleanup on `SIGINT`/`SIGTERM` — no orphaned offline runners
- One-command horizontal scaling via Docker Compose

## Repository layout

| File | Purpose |
|------|---------|
| `Dockerfile` | Builds the runner image |
| `start.sh` | Entrypoint — registers the runner, traps signals, unregisters on stop |
| `docker-compose.yml` | Scales multiple runners with shared config |

## Prerequisites

- Docker (and Docker Compose v2 for the scaling workflow)
- A GitHub repository or organization where you can add self-hosted runners
- A short-lived **registration token** (see below)

## 1. Get a registration token

The runner registers with a short-lived token (valid ~1 hour).

1. Go to your repo/org **Settings → Actions → Runners**
2. Click **New self-hosted runner**
3. Copy the token shown in the configuration step (the value after `--token`)

> [!IMPORTANT]
> The registration token is a secret and expires quickly. Do **not** commit it. Pass it in at runtime via the `REGISTRATION_TOKEN` environment variable.

## 2. Build the image

```bash
docker build -t custom-github-runner:latest .
```

Use a different runner version if needed:

```bash
docker build --build-arg RUNNER_VERSION="2.335.1" -t custom-github-runner:latest .
```

Check the [latest releases](https://github.com/actions/runner/releases) for the current version.

## 3. Run a single runner

```bash
docker run -d --restart always --name github-runner \
  -e REPO_URL="https://github.com/<username>/<repo_name>" \
  -e REGISTRATION_TOKEN="<github-runner-token>" \
  custom-github-runner:latest
```

| Variable | Description |
|----------|-------------|
| `REPO_URL` | Full URL of the repo or org to attach the runner to |
| `REGISTRATION_TOKEN` | Short-lived registration token from GitHub |

Because runners are ephemeral, the container exits after completing a job. `--restart always` brings it back up to register again with a fresh job slot.

## 4. Scale with Docker Compose

Copy the env template and fill in your values:

```bash
cp .env.example .env
# edit .env — set REPO_URL and REGISTRATION_TOKEN
```

`docker-compose.yml` reads these via `${REPO_URL}` / `${REGISTRATION_TOKEN}`, and Compose auto-loads `.env` (git-ignored). Launch multiple parallel runners:

```bash
docker compose up -d --scale runner=3
```

Tear everything down (containers deregister via the cleanup trap):

```bash
docker compose down
```

> [!TIP]
> Keep secrets out of `docker-compose.yml`. Use a `.env` file (git-ignored) and reference variables with `${REGISTRATION_TOKEN}`, or pass them through your shell environment.

## How it works

`start.sh` runs as the container entrypoint:

1. Reads `REPO_URL` and `REGISTRATION_TOKEN` from the environment
2. Registers the runner with `--ephemeral --replace`, named after the container hostname
3. Traps `SIGINT`/`SIGTERM` to remove the runner from GitHub on shutdown
4. Starts `run.sh` to listen for and execute a job

## Security notes

- Never commit registration tokens, PATs, or `.env` files — inject secrets at runtime
- The `docker` user has passwordless `sudo` inside the container; only run trusted workflows
- Self-hosted runners on **public** repositories are risky — forked PRs can run arbitrary code. Prefer private repos, or restrict workflow triggers accordingly
- Ephemeral runners reduce state-leakage between jobs; pair with a fresh container per job for stronger isolation

## License

MIT
