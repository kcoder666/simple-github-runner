# Simple GitHub Runner

[![Build](https://github.com/kcoder666/simple-github-runner/actions/workflows/docker-build.yml/badge.svg)](https://github.com/kcoder666/simple-github-runner/actions/workflows/docker-build.yml)

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
- A **Personal Access Token** (or GitHub App token) to mint registration tokens (see below)

## 1. Create a Personal Access Token

Because runners are **ephemeral**, they re-register on every (re)start. Registration tokens are single-use and expire in ~1 hour, so a static one would `404` on the second registration. Instead, the runner mints a fresh registration token at startup using a PAT.

Create a token with permission to manage self-hosted runners:

- **Repo runner** — a fine-grained PAT scoped to the repo with **Administration: Read and write**
- **Org runner** — a classic PAT with the **`manage_runners:org`** scope

> [!IMPORTANT]
> The PAT is a long-lived secret. Do **not** commit it. Pass it in at runtime via the `GITHUB_PAT` environment variable.

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
  -e GITHUB_PAT="<github-pat>" \
  custom-github-runner:latest
```

| Variable | Description |
|----------|-------------|
| `REPO_URL` | Full URL of the repo **or** org to attach the runner to |
| `GITHUB_PAT` | PAT used to mint a fresh registration token on each startup |

`REPO_URL` accepts either a repo (`.../owner/repo`) or an org (`.../org`). An org URL registers an **org-level** runner that serves every repo in the org; `start.sh` picks the right GitHub API scope automatically.

Because runners are ephemeral, the container exits after completing a job. `--restart always` brings it back up, and `start.sh` mints a new registration token from the PAT to register a fresh job slot.

## 4. Scale with Docker Compose

Copy the env template and fill in your values:

```bash
cp .env.example .env
# edit .env — set GITHUB_PAT, plus ORG_URL and/or REPO_URL_n
```

One config covers three targeting modes via Compose **profiles**:

```bash
# All repos in an org (scale to N concurrent runners)
docker compose --profile org up -d --scale org-runner=3

# A specific list of repos (one runner per repo)
docker compose --profile repos up -d

# Both at once
docker compose --profile org --profile repos up -d
```

- **`org` profile** → the `org-runner` service, driven by `ORG_URL`. Use `--scale org-runner=N` for parallelism.
- **`repos` profile** → one service per repo (`repo-1`, `repo-2`, …), driven by `REPO_URL_1`, `REPO_URL_2`, …. Add more by copying a `repo-*` service in `docker-compose.yml` and a matching `REPO_URL_n` in `.env`. Repos may live under different owners.

Nothing starts without a selected profile, so an unconfigured mode never launches a broken container. Tear everything down (containers deregister via the cleanup trap):

```bash
docker compose --profile org --profile repos down
```

> [!TIP]
> Keep secrets out of `docker-compose.yml`. Use a `.env` file (git-ignored) and reference variables with `${GITHUB_PAT}`, or pass them through your shell environment.

## How it works

`start.sh` runs as the container entrypoint:

1. Reads `REPO_URL` and `GITHUB_PAT` from the environment, deriving the repo/org API scope from the URL
2. Mints a fresh registration token via the GitHub API (`gh api`), so every restart self-heals
3. Registers the runner with `--ephemeral --replace`, named after the container hostname
4. Traps `SIGINT`/`SIGTERM` to mint a remove token and deregister the runner on shutdown
5. Starts `run.sh` to listen for and execute a job

## Security notes

- Never commit registration tokens, PATs, or `.env` files — inject secrets at runtime
- The `docker` user has passwordless `sudo` inside the container; only run trusted workflows
- Self-hosted runners on **public** repositories are risky — forked PRs can run arbitrary code. Prefer private repos, or restrict workflow triggers accordingly
- Ephemeral runners reduce state-leakage between jobs; pair with a fresh container per job for stronger isolation

## License

MIT
