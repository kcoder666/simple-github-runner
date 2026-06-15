FROM ubuntu:24.04

# Avoid prompts
ARG DEBIAN_FRONTEND=noninteractive

# Install dependencies
RUN apt-get update && apt-get install -y \
        curl \
        sudo \
        jq \
        ca-certificates \
        gnupg \
        git \
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

