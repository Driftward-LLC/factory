ARG FACTORY_RELEASE_COMMIT=development-unversioned
ARG FACTORY_RELEASE_VERSION=local

FROM node:22-bookworm-slim AS build

WORKDIR /app
COPY . .

RUN npm ci --prefix factory/console \
  && npm run build --prefix factory/console \
  && rm -rf factory/console/node_modules

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

FROM ghcr.io/hatchet-dev/hatchet/hatchet-lite@sha256:0fc77a4779a58778abafb20d2b9077830dc0ebb984a00c8d31cc288b0b398bf6 AS hatchet
FROM postgres:15.6@sha256:0b723c153bca6ffdc3d8084d3b4d493a945b955456bb4318c74f7a66d371a1fd AS postgres
FROM otel/opentelemetry-collector-contrib@sha256:fd328de2552466ad78385e1b1289c3f2402b1c45f265b252aab1955b42845ac1 AS collector
FROM nousresearch/hermes-agent@sha256:32d095e84190ae7266afbb361579e6921f2c633e899f5eacfb88111b9c851a22 AS assembled

USER root
# Preserve Hermes' own interpreter and Node; Factory keeps the Node 22 it builds with.
RUN mv /usr/local/bin/node /usr/local/bin/hermes-node
COPY --from=build /usr/local/bin/node /usr/local/bin/node
COPY --from=postgres /usr/local/bin/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY --from=hatchet /hatchet-lite /hatchet-admin /hatchet-migrate /
COPY --from=hatchet /static-assets /static-assets
COPY --from=hatchet /entrypoint.sh /entrypoint.sh
COPY --from=collector /otelcol-contrib /otelcol-contrib

# Install PostgreSQL 15 against the same Debian libraries as Hermes.
RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates curl gnupg gosu locales \
  && mkdir -p /usr/share/postgresql-common/pgdg \
  && curl -fsS https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
  && gpg --show-keys --with-colons /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc | grep -q '^fpr:::::::::B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8:' \
  && printf 'deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt trixie-pgdg main\n' > /etc/apt/sources.list.d/pgdg.list \
  && apt-get update \
  && apt-get install -y --no-install-recommends postgresql-15 \
  && rm -rf /var/lib/apt/lists/* /var/lib/postgresql/15/main \
  && localedef -i en_US -c -f UTF-8 -A /usr/share/locale/locale.alias en_US.UTF-8 \
  && mkdir -p /docker-entrypoint-initdb.d /var/lib/postgresql/data \
  && chown postgres:postgres /var/lib/postgresql/data \
  && chmod 1777 /var/lib/postgresql/data \
  && ln -s /usr/local/bin/hermes-node /opt/hermes/bin/node


RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates curl git openssh-client python3 python3-venv tini \
  && rm -rf /var/lib/apt/lists/* \
  && ln -sf /usr/local/bin/node /usr/bin/node

COPY --from=build /app/hatchet/bridge/requirements.txt /tmp/factory-requirements.txt
RUN /usr/bin/python3 -m venv /opt/driftward-factory \
  && /opt/driftward-factory/bin/pip install --no-cache-dir --requirement /tmp/factory-requirements.txt \
  && rm /tmp/factory-requirements.txt

# Reset upstream image defaults, including its VOLUME /opt/data. Only the
# Hermes service mounts that data volume; no anonymous volumes in other roles.
FROM scratch AS runtime
COPY --from=assembled / /

ARG FACTORY_RELEASE_COMMIT
ARG FACTORY_RELEASE_VERSION

LABEL org.opencontainers.image.source="https://github.com/Driftward-LLC/factory" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.revision="$FACTORY_RELEASE_COMMIT" \
      org.opencontainers.image.version="$FACTORY_RELEASE_VERSION"

WORKDIR /app
COPY --from=build /app /app
COPY --from=build /usr/local/share/doc/node /usr/local/share/doc/node

RUN printf '{"commit":"%s","version":"%s"}\n' "$FACTORY_RELEASE_COMMIT" "$FACTORY_RELEASE_VERSION" \
  > /app/hatchet/bridge/RELEASE_METADATA.json

ENV NODE_ENV=production \
    PATH=/opt/driftward-factory/bin:/usr/lib/postgresql/15/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FACTORY_RELEASE_COMMIT=$FACTORY_RELEASE_COMMIT \
    FACTORY_RELEASE_VERSION=$FACTORY_RELEASE_VERSION \
    PYTHONPATH=/app/hatchet/bridge:/app/hatchet/event-ingress:/app/hatchet/factory-lifecycle-writer \
    FACTORY_HATCHET_DOCTOR_SCRIPT=/app/tools/factory-hatchet-doctor.py \
    FACTORY_HATCHET_DESIRED_STATE=/app/hatchet/desired-state/factory-hatchet.json

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/factory/factory", "status"]
