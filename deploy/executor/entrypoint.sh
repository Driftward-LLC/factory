#!/bin/sh
set -eu
umask 077
mkdir -p /opt/data/work
if [ -n "${POSTHOG_API_KEY:-}" ]; then
  hermes plugins enable observability/posthog >/dev/null
fi
if [ -n "${DRIFTWARD_HERMES_EXPECTED_GIT_AUTHOR_NAME:-}" ]; then
  git config --global user.name "$DRIFTWARD_HERMES_EXPECTED_GIT_AUTHOR_NAME"
  git config --global user.email "${DRIFTWARD_HERMES_EXPECTED_GIT_AUTHOR_EMAIL:?Configure the executor bot identity}"
  git config --global credential.helper '!gh auth git-credential'
fi
exec "$@"
