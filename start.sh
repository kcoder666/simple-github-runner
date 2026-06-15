#!/bin/bash

# Configuration variables
REPOSITORY=$REPO_URL
TOKEN=$REGISTRATION_TOKEN

echo "Configuring GitHub Actions Runner..."
./config.sh --url ${REPOSITORY} --token ${TOKEN} --name "$(hostname)" --work "_work" --replace --ephemeral

cleanup() {
    echo "Removing runner..."
    ./config.sh remove --token ${TOKEN}
}

# Trap termination signals to clean up the runner from your GitHub UI
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

# Start listening for jobs
./run.sh & wait $!

