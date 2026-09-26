# Build from the repo root:  docker build -f controller/Dockerfile -t github-runner-controller .
FROM python:3.12-slim

ARG DEBIAN_FRONTEND=noninteractive

# Docker CLI + buildx: the controller builds the runner image with BuildKit.
# Runtime container management goes through the Docker SDK over the same socket.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli docker-buildx-plugin \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY controller/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY controller/app ./app
# Build context for the runner image, so the controller can (re)build it.
COPY runner ./runner

ENV DATA_DIR=/data \
    RUNNER_BUILD_CONTEXT=/app/runner \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
