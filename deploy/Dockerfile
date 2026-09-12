ARG FACTORY_RELEASE_COMMIT=development-unversioned
ARG FACTORY_RELEASE_VERSION=local

FROM node:22-bookworm-slim AS build

WORKDIR /app
COPY . .

RUN npm ci --prefix hatchet/workers \
  && npm run build --prefix hatchet/workers \
  && mkdir -p /app/worker-triggers \
  && npm exec --prefix hatchet/workers -- esbuild \
    /app/hatchet/workers/bridge-executor-worker/src/triggers/push-accepted-event.ts \
    --bundle \
    --platform=node \
    --format=cjs \
    --target=node18 \
    --line-limit=120 \
    --legal-comments=none \
    --outfile=/app/worker-triggers/push-accepted-event.js \
  && npm prune --omit=dev --prefix hatchet/workers

FROM node:22-bookworm-slim AS runtime

RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates curl git openssh-client python3 python3-venv tini \
  && rm -rf /var/lib/apt/lists/* \
  && ln -sf /usr/local/bin/node /usr/bin/node

COPY --from=build /app/hatchet/bridge/requirements.txt /tmp/factory-requirements.txt
RUN python3 -m venv /opt/driftward-factory \
  && /opt/driftward-factory/bin/pip install --no-cache-dir --requirement /tmp/factory-requirements.txt \
  && rm /tmp/factory-requirements.txt

ARG FACTORY_RELEASE_COMMIT
ARG FACTORY_RELEASE_VERSION

LABEL org.opencontainers.image.source="https://github.com/Driftward-LLC/factory" \
      org.opencontainers.image.revision="$FACTORY_RELEASE_COMMIT" \
      org.opencontainers.image.version="$FACTORY_RELEASE_VERSION"

WORKDIR /app
COPY --from=build /app /app

RUN printf '{"commit":"%s","version":"%s"}\n' "$FACTORY_RELEASE_COMMIT" "$FACTORY_RELEASE_VERSION" \
  > /app/hatchet/bridge/RELEASE_METADATA.json

ENV NODE_ENV=production \
    PATH=/opt/driftward-factory/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/hatchet/bridge:/app/hatchet/event-ingress:/app/hatchet/factory-lifecycle-writer \
    FACTORY_HATCHET_DOCTOR_SCRIPT=/app/tools/factory-hatchet-doctor.py \
    FACTORY_HATCHET_DESIRED_STATE=/app/hatchet/desired-state/factory-hatchet.json

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/factory/factory", "status"]
