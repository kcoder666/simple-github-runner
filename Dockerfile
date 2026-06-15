FROM ubuntu:24.04

# Avoid prompts
ARG DEBIAN_FRONTEND=noninteractive

# Install base dependencies
RUN apt-get update && apt-get install -y \
        curl \
        sudo \
        jq \
        ca-certificates \
        gnupg \
        git \
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

# Create a non-root user
RUN useradd -m docker && usermod -aG sudo docker && echo "docker ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

USER docker
WORKDIR /home/docker/actions-runner

# Download and extract the GitHub Actions runner (verify latest version on GitHub)
ARG RUNNER_VERSION="2.335.1"
RUN curl -o actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz -L \
https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz \
&& tar xzf ./actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz \
&& rm actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz

# Install execution dependencies
RUN sudo ./bin/installdependencies.sh

COPY --chown=docker:docker start.sh start.sh
RUN chmod +x start.sh

ENTRYPOINT ["./start.sh"]

