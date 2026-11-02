# Naming generation boundary

This release uses the `factory-neutral-v1` naming generation for internal
workflow events, configuration, paths, resource labels, and state namespaces.
Fresh installations record `naming_generation` in `.factory-install.json`.
Installation operations and snapshot restore require that exact generation
before starting containers or importing state.

Installation operations and normal snapshot restore still refuse installations
without this marker. Do not add it manually: source renames do not migrate
queued events, workflow subscriptions, hash identities, or executor receipts.
The only supported transition is the cold, stopped profile migration in
[MIGRATE.md](MIGRATE.md), using the preserved source bundle and a separately
reviewed private naming map. It prepares a new unified project and retains the
original installation, bundle, volumes and backup for rollback.

The transition refuses active execution, unpublished ingress, unacknowledged
bridge delivery/inspection, active verification leases, nonterminal reviews,
undelivered lifecycle rows (including dead letters), pending Hatchet queues,
and enabled schedules. It does not drain queues, archive dead letters, disable
schedules, rewrite historical records, or interpret missing receipts as success.
Resolve those prerequisites through separately reviewed original-release operator
procedures before taking the final cold backup.

The three copied SQLite stores gain the generation marker only after these
checks. Existing tables, event IDs, UUIDs, payloads, hashes, lifecycle markers,
foreign keys, and timestamps retain their original values. A supplemental
per-attempt mapping retains historical agent trace links; fresh attempts use the
current trace namespace. Existing work evidence and human-time request IDs are
resolved through their stored semantic identities, preserving idempotency across
the seed change. Payload-free decision events did not retain their occurrence
keys: a later repeated observation can append another audit decision, but it
cannot authorize execution or provider effects. Original audit events remain.

The complete Hatchet PostgreSQL history and subscriptions are logically restored
without rewriting rows. Both source-copy and restored scheduler stores must be
quiescent. New workers register current-generation event subscriptions after a
separate activation approval; no queued old event is translated into new work.
The old subscriptions remain historical and must not be re-enabled separately.

A private cold host gateway journal archive is retained byte-for-byte under the
new installation's `migration-evidence/`. Incomplete intents and cancellation
records remain evidence; they are never copied into an active journal, stamped
with a new generation, or treated as execution permission. The target gateway
is explicitly **unbound**. Full production startup refuses this receipt until a
separately reviewed executor-binding change is implemented. Original journal
ownership, credentials and runtime supervision remain with the source.

For fresh deployments, use a new private installation directory, a unique
Compose project, empty volumes and reviewed organization configuration. Real
execution remains disabled until explicitly approved.

Historical source/systemd installers are disabled before taking locks, publishing
files, changing permissions, or stopping/starting services. Their renamed unit
inventories cannot establish that an existing installation has no older writers.
This also applies to the source Docker wrapper installer and public webhook
installer. Use the portable installation path for fresh deployments. Re-enabling
a source rollout requires its own reviewed provisioning and migration design;
there is no environment variable or marker override for this refusal.

External executor plugins keep their own verified package identities. Profile
configuration obtains the skill namespace from the pinned plugin archive and
returns `skill_namespace`; it also stores the value in the executor profile.
When binding an executor profile, set `FACTORY_HERMES_SKILL_NAMESPACE` in the
gateway environment to that exact returned namespace. A selected executor
profile refuses to run without a valid explicit namespace. This configuration
contains an external plugin identifier, not credentials.
