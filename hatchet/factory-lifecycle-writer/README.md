# Factory Lifecycle Writer

This root-owned, loopback-only service is the dedicated Linear client for
the Hatchet Lite lifecycle. Hatchet workers, the bridge, ingress, and Hermes
never receive its client credentials or access token.

## Purpose

The lifecycle writer turns factory execution state into safe, human-readable
Linear updates without giving the rest of the automation stack permission to
write to Linear. It solves three problems:

- It keeps Linear credentials isolated in one root-owned service instead of
  distributing write access to workers, ingress, the bridge, or Hermes.
- It converts bridge and ingress observations into durable, deduplicated Linear
  comments so operators can see whether a work item was accepted, blocked,
  verifying, failed, or ready for human review.
- It prevents misleading or stale updates by suppressing dry-run effects,
  avoiding disabled-project noise, dead-lettering ambiguous issue references,
  and only announcing success after verified draft-PR evidence exists.

It polls only `GET /lifecycle/observations` (a sanitized ingress projection)
and the authenticated bridge job ledger. It writes a durable SQLite outbox at
`/var/lib/driftward-factory-lifecycle-writer/outbox.db`, verifies its source-defined
client identity, workspace, and team access before every scan, and creates at
most one marked Linear comment per issue/job/phase. Its actor, workspace, and
team IDs are currently fixed in `factory_lifecycle_writer/linear.py`.
Connecting a different workspace requires a reviewed source-policy change;
supplying another token alone does not change those identity checks.

`dry_run` source snapshots are written as permanently `suppressed` outbox rows;
switching Factory to real mode cannot replay them. Internal draft-PR inspection
jobs are not lifecycle-comment sources. Missing immutable Linear issue UUIDs
are dead-lettered rather than resolved by identifier. The only
routing failure currently commentable is
`linear_issue_project_execution_policy_invalid`; intentional
`linear_issue_project_execution_disabled` observations stay silent.

Reviewed historical dead letters can be archived through the root-only Factory
operator. The archive path is one record at a time, snapshot-fenced, and
audited; it retains the original reason code and never exposes the stored
comment body to the browser console.

```bash
factory lifecycle dead-letters list
factory lifecycle dead-letters preview OUTBOX-KEY
factory lifecycle dead-letters archive OUTBOX-KEY \
  --reason "reviewed historical record" \
  --snapshot-digest PREVIEW_DIGEST \
  --yes
```

## Success Boundary

The writer treats `effect_committed` and legacy `completed` bridge states as
non-success artifact records and suppresses their comments. It writes one
sanitized `verification_pending` comment when a real draft PR exists, so a
stall is visible without claiming success. It emits the real lifecycle
succeeded/ready comment only for `ready_for_human_review`, after the bridge
has validated and stored a green, hashed draft-PR verification receipt.
Verification failures produce sanitized failed comments with stable reason
codes; raw provider output and credentials never enter the outbox or Linear.

Install or refresh after bridge and ingress:

```bash
bash hatchet/factory-lifecycle-writer/install.sh
systemctl status driftward-factory-lifecycle-writer.service --no-pager
systemctl status driftward-factory-linear-token.timer --no-pager
curl -fsS http://127.0.0.1:19880/status
```

The root-only token service reads `/root/.config/linear/factory_client_{id,secret}`
and atomically publishes only `LINEAR_FACTORY_ACCESS_TOKEN` into
`/etc/driftward/factory-lifecycle-writer.env` (mode `0600`). Never print,
copy, or commit either credential or token.

In the Docker deployment, the token renewer runs as the
`factory-linear-token-refresh` Compose service. It reads
`FACTORY_LINEAR_CLIENT_ID` and `FACTORY_LINEAR_CLIENT_SECRET` from the protected
`/etc/driftward/factory-linear-token.env` file and writes only the refreshed
access token into the mounted lifecycle-writer env file. Do not mount root's
Linear credential directory into the Factory containers.
