# Bundled components

Factory source remains MIT licensed. Bundling does not relicense third-party
components. The image includes the following upstream notices:

- Hatchet: `Hatchet.LICENSE`, from https://github.com/hatchet-dev/hatchet/blob/main/LICENSE.
- OpenTelemetry Collector Contrib 0.161.0: `OpenTelemetry.LICENSE` and
  `OpenTelemetry.NOTICE`, from the corresponding upstream release tag.
- Hermes: upstream source and license remain in `/opt/hermes`.
- PostgreSQL and Debian packages: `/usr/share/doc/*/copyright`.
- Node.js: `/usr/local/share/doc/node/LICENSE`.

The Dockerfile records upstream image digests. Each published Factory image
pins the combined build; inspect `dpkg-query -W postgresql-15` in that image
for its PostgreSQL minor release. Fresh deployments use the native Debian 13
PostgreSQL package. Existing installations retain their original database image
and storage ownership until a separately reviewed migration.
