FROM node:22-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392
ARG FACTORY_RELEASE_COMMIT
LABEL org.opencontainers.image.revision=$FACTORY_RELEASE_COMMIT \
      io.driftward.factory.ui-preview="1"
COPY tools/fixtures/ui-verifier/preview.cjs /preview.cjs
USER node
ENTRYPOINT ["node", "/preview.cjs"]
