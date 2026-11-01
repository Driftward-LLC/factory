# Organization and execution configuration

The same public Factory image runs for every installation. Organization identity,
project routing, repository scope and base branches are deployment inputs. The
image's checked-in policies deny all execution and review. A fresh portable
installation boots in dry-run mode with both integrations disabled.

`bootstrap.py init --apply` creates four 0600 JSON files inside the private
`INSTALL/config/` directory. Compose mounts only that directory read-only at
`/etc/factory/config` for application roles. `env/` holds secrets and is never
mounted as a directory. PostgreSQL, Hatchet, and idle Hermes do not receive the
organization configuration. All configured application roles validate before
starting; malformed or missing configured files fail startup.

| File | Environment variable | Contract |
| --- | --- | --- |
| `organization.json` | `FACTORY_ORGANIZATION_CONFIG` | `factory-organization-v1` |
| `routing-registry.json` | `HATCHET_INTAKE_ROUTING_REGISTRY_PATH` | [version 2, project routes](examples/routing-registry.schema.json) |
| `verifier-policy.json` | `HATCHET_VERIFIER_POLICY_PATH` | [version 1, repository verification](examples/verifier-policy.schema.json) |
| `independent-review-policy.json` | `HATCHET_INDEPENDENT_REVIEW_POLICY_PATH` | [factory-independent-review-policy-v1](examples/independent-review-policy.schema.json) |

See [the organization schema](examples/organization.schema.json), the
[configured organization example](examples/organization.configured.example.json),
and [disabled defaults](examples/organization.json). Values are examples only;
provision the real workspace, team and App actor IDs from the installation.
`linear: null` disables Linear and `github.enabled: false` disables GitHub.
An absent organization environment variable also disables integrations in standalone
processes. A configured path must exist and pass validation.

A route maps one canonical Linear project UUID to `owner/repo` and an explicit
`execution_enabled` boolean. Empty routes authorize no project. Each project
and repository may appear only once, including disabled routes; repository
uniqueness ignores case. A registry containing only disabled routes is valid
and admits no execution. Repository scope also requires an explicit
`HATCHET_INTAKE_ALLOWED_REPOS` entry in `env/hatchet-workers.env` and a matching
verifier policy, including for a synthetic dry-run admission. Real execution
also requires a gateway
`DRIFTWARD_HERMES_ALLOWED_REPOS` entry, and, when review is enabled, a
matching independent review repository. Keep these lists aligned. Repo entries
must have the form `owner/repository`; duplicates and empty entries are invalid.
Schema files document field shapes; startup validation also enforces unique
project mappings, unique repository routes, and repository policies.
No source organization or repository is implicitly allowed.

Verifier policies retain the existing fields: `id`, `repo`, `expected_base`,
`allowed_changed_paths`, `forbidden_changed_paths`, `required_local_commands`,
and `secret_check_command`. Set `expected_base` to the reviewed base branch for
that repository. Commands and path rules are execution authority: review them
with the operator. Independent review retains `enabled`,
`automatic_repairs_allowed`, `max_passes` (2), and `repositories`.

Identity files contain no credentials. Provision webhook signing secrets only
in `env/hatchet-event-ingress.env`; missing signing secrets reject delivery.
Linear App/OAuth credentials belong only to the token-refresh role, which
publishes the bounded actor token to the lifecycle writer. Provider credentials,
GitHub App private keys and browser/MCP credentials belong only to the dedicated
executor. Workers receive Hatchet and bridge credentials only. Review source
renewal reads the current review policy on every invocation, rejects cached
scope that no longer matches, and transports only the approved repository list
to native Hermes before requesting a read-only token.

To validate supplied configuration without starting a service:

```bash
FACTORY_ORGANIZATION_CONFIG=/path/config/organization.json \
HATCHET_INTAKE_ROUTING_REGISTRY_PATH=/path/config/routing-registry.json \
HATCHET_VERIFIER_POLICY_PATH=/path/config/verifier-policy.json \
HATCHET_INDEPENDENT_REVIEW_POLICY_PATH=/path/config/independent-review-policy.json \
PYTHONPATH=hatchet/bridge python3 -m factory_organization
```

Cold backups include the exact four files and checksums; restore relocates only
the config directory path and preserves their bytes. Migration preview binds
supplied configuration hashes into the reviewed digest. It does not infer
historical identity or replace historical policy with new source defaults.

For a pre-configuration production installation, retain its original bundle and
complete cold backup. Prepare a separate private directory containing exactly the
four reviewed JSON files, then use the new bundle's existing
[profile migration](MIGRATE.md) with `--configuration /path/config`. The command
copies them only into a **new, stopped** installation and preserves the source
bundle, backup, image history, and rollback state. Review the prepared deployment
before approving any integration or executor activation. Existing unified
installations whose deployment hashes change require a separately reviewed
transition; image-only upgrade continues to refuse changed Compose inputs.

Existing pending real jobs without `payload.organization_binding` require explicit
operator resolution and new admission from an observed issue before execution.
The binding records the original observed `workspace_id`, `team_id`, and
`team_key`; it must match the current configured identity at admission and before
an effect. Configuration changes do not backfill historical jobs or grant them
new identity. Historical records and receipts remain readable.
