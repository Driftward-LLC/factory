# syntax=docker/dockerfile:1
FROM node:22-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392
ARG FACTORY_RELEASE_COMMIT
LABEL org.opencontainers.image.source="https://github.com/Driftward-LLC/factory" \
      org.opencontainers.image.revision=$FACTORY_RELEASE_COMMIT \
      io.driftward.factory.ui-verifier="1"
WORKDIR /opt/verifier
COPY deploy/ui-verifier/package*.json ./
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/proxy_ca; fi; \
    npm ci --ignore-scripts --no-audit --no-fund
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/browsers
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/proxy_ca; fi; \
    node node_modules/playwright/cli.js install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*
COPY deploy/ui-verifier/*.cjs ./
COPY deploy/ui-verifier/response.schema.json ./
USER node
ENTRYPOINT ["node", "/opt/verifier/runner.cjs"]
