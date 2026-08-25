FROM node:22-bookworm-slim AS build

WORKDIR /app
COPY . .

RUN npm ci --prefix hatchet/workers \
  && npm run build --prefix hatchet/workers \
  && npm prune --omit=dev --prefix hatchet/workers \
  && mkdir -p /app/worker-triggers \
  && cp /app/hatchet/workers/bridge-executor-worker/dist/triggers/push-accepted-event.js /app/worker-triggers/push-accepted-event.js

FROM node:22-bookworm-slim AS runtime

RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates curl git openssh-client python3 tini \
  && rm -rf /var/lib/apt/lists/* \
  && ln -sf /usr/local/bin/node /usr/bin/node

WORKDIR /app
COPY --from=build /app /app

ENV NODE_ENV=production \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/hatchet/bridge:/app/hatchet/event-ingress:/app/hatchet/factory-lifecycle-writer \
    FACTORY_HATCHET_DOCTOR_SCRIPT=/app/tools/factory-hatchet-doctor.py \
    FACTORY_HATCHET_DESIRED_STATE=/app/hatchet/desired-state/factory-hatchet.json

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/factory/factory", "status"]
