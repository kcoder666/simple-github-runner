FROM ubuntu:24.04

# Avoid prompts
ARG DEBIAN_FRONTEND=noninteractive

# Install base dependencies
# unzip/zip: required by actions such as oven-sh/setup-bun that extract .zip releases
# wget, tar, xz-utils, build-essential: common tooling many CI actions and installers expect
RUN apt-get update && apt-get install -y \
        curl \
        wget \
        sudo \
        jq \
        unzip \
        zip \
        tar \
        xz-utils \
        ca-certificates \
        gnupg \
        git \
        build-essential \
        python3 \
        python3-pip \
        python3-venv \
        && rm -rf /var/lib/apt/lists/*

# Install GitHub CLI (gh) from the official apt repository
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# Install Playwright browser system libraries (Ubuntu 24.04 / noble package names)
RUN apt-get update && apt-get install -y \
        fonts-liberation \
        libasound2t64 \
        libatk-bridge2.0-0t64 \
        libatk1.0-0t64 \
        libatspi2.0-0t64 \
        libcairo2 \
        libcups2t64 \
        libdbus-1-3 \
        libdrm2 \
        libgbm1 \
        libglib2.0-0t64 \
        libgtk-3-0t64 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libx11-6 \
        libxcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        && rm -rf /var/lib/apt/lists/*

# Install the Docker CLI (client only, no daemon). Container actions
# (runs.using: "docker", e.g. appleboy/ssh-action) make the runner shell out to
# `docker build`/`docker run`; without the CLI they fail with "docker: command
# not found". The daemon is shared from the host via a bind-mounted socket
# (see docker-compose.yml) — we do NOT run Docker-in-Docker.
RUN install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y docker-ce-cli docker-buildx-plugin \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user
RUN useradd -m docker && usermod -aG sudo docker && echo "docker ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

USER docker
WORKDIR /home/docker/actions-runner

# Download and extract the GitHub Actions runner for the build host's arch.
# The controller rebuilds this image with the latest RUNNER_VERSION automatically
# (GitHub stops routing jobs to runners that fall too far behind a release).
# Arch comes from dpkg rather than TARGETARCH so legacy (non-BuildKit) builds work.
ARG RUNNER_VERSION="2.335.1"
LABEL ghr.runner_version="${RUNNER_VERSION}"
RUN case "$(dpkg --print-architecture)" in \
        amd64) arch=x64 ;; \
        arm64) arch=arm64 ;; \
        *) echo "unsupported arch: $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o runner.tar.gz \
        "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-${arch}-${RUNNER_VERSION}.tar.gz" \
    && tar xzf runner.tar.gz \
    && rm runner.tar.gz

# Install execution dependencies
RUN sudo ./bin/installdependencies.sh

COPY --chown=docker:docker start.sh start.sh
RUN chmod +x start.sh

ENTRYPOINT ["./start.sh"]

