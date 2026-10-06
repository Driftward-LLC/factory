# Product-owned executor; no host wrapper, Docker socket, or shared Hermes state.
ARG FACTORY_IMAGE=driftward/factory:local
FROM ${FACTORY_IMAGE} AS factory

# Public, reviewed source; the published 0.1.1 package lacks the remote client.
# Only the bundled stdio client (not Capykit's hosted services) ships at runtime.
FROM factory AS capykit-client
WORKDIR /opt/factory/capykit
RUN git init . \
    && git remote add origin https://github.com/Driftward-LLC/capykit.git \
    && git fetch --depth=1 origin eadda266ba9990927c30d84b272bc42b6c6f8573 \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = eadda266ba9990927c30d84b272bc42b6c6f8573 \
    && npm ci --include=dev --ignore-scripts \
    && ./node_modules/.bin/esbuild src/mcp/index.ts --bundle --platform=node \
      --format=esm --target=node22 \
      --banner:js="import { createRequire } from 'node:module'; const require = createRequire(import.meta.url);" \
      --outfile=dist/mcp.js \
    && git rev-parse HEAD > dist/SOURCE_COMMIT

FROM nousresearch/hermes-agent@sha256:e14e581b657596eda78487ce01ca2d61c7ed8675bb7452282581e36df734412e

USER root
RUN apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /usr/lib/factory /run/factory-executor /opt/data \
    && mv /usr/bin/gh /usr/lib/factory/gh-real \
    && chown 10000:10000 /run/factory-executor /opt/data
COPY --from=factory /app/hatchet/workers /app/hatchet/workers
COPY --from=capykit-client /opt/factory/capykit/dist/mcp.js /opt/factory/capykit/mcp.mjs
COPY --from=capykit-client /opt/factory/capykit/dist/SOURCE_COMMIT /opt/factory/capykit/SOURCE_COMMIT
COPY --from=capykit-client /opt/factory/capykit/LICENSE /opt/factory/capykit/LICENSE
COPY --chmod=0755 deploy/executor/entrypoint.sh /usr/local/bin/factory-executor
COPY --chmod=0755 deploy/executor/gh /usr/local/bin/gh
COPY deploy/executor/plugins/posthog /opt/hermes/plugins/observability/posthog
RUN chmod -R a+rX /app/hatchet/workers /opt/hermes/plugins/observability/posthog

ARG FACTORY_RELEASE_COMMIT=development-unversioned
ARG FACTORY_RELEASE_VERSION=local
LABEL org.opencontainers.image.source="https://github.com/Driftward-LLC/factory" \
      org.opencontainers.image.revision="$FACTORY_RELEASE_COMMIT" \
      org.opencontainers.image.version="$FACTORY_RELEASE_VERSION"

ENV HERMES_HOME=/opt/data HOME=/opt/data \
    PATH=/opt/hermes/.venv/bin:/usr/local/bin:/usr/bin:/bin \
    GH_CONFIG_DIR=/opt/data/gh-config GIT_CONFIG_GLOBAL=/opt/data/gitconfig \
    DRIFTWARD_HERMES_REAL_GH=/usr/lib/factory/gh-real \
    DRIFTWARD_HERMES_RUNTIME_MODE=local-executor \
    DRIFTWARD_HERMES_COMMAND=/opt/hermes/.venv/bin/hermes \
    DRIFTWARD_HERMES_GRPC_BIND_TARGET=unix:///run/factory-executor/hermes.sock \
    HATCHET_VERIFIER_POLICY_PATH=/app/hatchet/workers/verifier-policy.json
WORKDIR /opt/data
USER 10000:10000
ENTRYPOINT ["/usr/local/bin/factory-executor"]
CMD ["node", "/app/hatchet/workers/bridge-executor-worker/dist/hermes/hermes-gateway-server.js"]
