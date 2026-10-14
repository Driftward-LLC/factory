# syntax=docker/dockerfile:1
# Same reviewed Node base and locked CLI/browser dependencies as PR 65.
FROM node:22-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392
LABEL io.driftward.factory.independent-reviewer="1"
WORKDIR /opt/factory-reviewer
COPY deploy/independent-reviewer/package*.json ./
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/proxy_ca; fi; \
    npm ci --ignore-scripts --no-audit --no-fund
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/browsers
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/proxy_ca; fi; \
    apt-get update && apt-get install -y --no-install-recommends python3 git \
    && node node_modules/playwright/cli.js install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*
ENV PATH=/opt/factory-reviewer/node_modules/.bin:$PATH
COPY deploy/independent-reviewer/runner.py /opt/factory-reviewer/runner.py
COPY deploy/independent-reviewer/source_server.py /opt/factory-reviewer/source_server.py
COPY deploy/independent-reviewer/browser_probe.cjs /opt/factory-reviewer/browser_probe.cjs
COPY deploy/independent-reviewer/response.schema.json /opt/factory-reviewer/response.schema.json
ENTRYPOINT ["python3", "/opt/factory-reviewer/runner.py"]
