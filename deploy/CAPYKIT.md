# Capykit tooling in the Factory executor

Factory owns agents, jobs, leases, retries and receipts. Capykit owns connected
accounts, grants, tool discovery and individual tool calls. Only the bundled
executor receives the caller credential; workers receive neither Capykit nor
provider credentials. No additional Factory service/container is needed.

The executor ships the existing Capykit stdio client from reviewed source
`eadda266ba9990927c30d84b272bc42b6c6f8573`, with its lockfile dependencies
bundled at build time. `/opt/factory/capykit/SOURCE_COMMIT` records provenance.
It does not install Capykit's hosted gateway or connector engine. The published
0.1.1 npm package is not a substitute for this remote-capable source pin.

## Optional setup (operator-owned, executor only)

1. In Capykit Access, issue a dedicated **expiring Factory client key**. The
   UI's legacy “agent key” identity is a tool caller, not an agent runtime.
   Grant only GitHub `getIssueInformation` on the approved connected account
   for the first canary. Do not grant writes, arbitrary API calls, or other
   actions. Discover and verify the deployed action name/schema before use.
2. Provision `CAPYKIT_API_KEY` and `CAPYKIT_BASE_URL` in the installation's
   mode-0600 `env/executor.env` through a secure operator session. Never put the
   key in chat, argv, the public identity envelope, workers, logs or Git.
   The URL must be an HTTPS origin (no path, query or embedded credentials).
   Recreate only an idle, dry-run installation using its normal `up` command.
3. Through `factory-portable.py --directory /srv/factory executor config edit`,
   add the following **entry** to `mcp_servers` in native Hermes config. Preserve
   existing entries/settings. Environment references are literal strings;
   Hermes resolves them at runtime and filters unrelated provider credentials.

```yaml
mcp_servers:
  capykit:
    command: node
    args: [/opt/factory/capykit/mcp.mjs, --remote]
    env:
      CAPYKIT_BASE_URL: '${CAPYKIT_BASE_URL}'
      CAPYKIT_API_KEY: '${CAPYKIT_API_KEY}'
    tools:
      include: [list_connections, list_connection_actions, run_connection_action]
    timeout: 60
    connect_timeout: 15
```

This is native stdio MCP invoking Capykit's HTTPS REST gateway, **not** a hosted
`/mcp` endpoint. The generic `run_connection_action` tool can perform writes if
granted: the include list limits tool exposure, not action authorization. The
dedicated key's read-only grants are the security boundary. Do not expand them
as part of initial activation. Workspace connector tools remain unexposed.

The Driftward public gateway origin is
`https://srv1379035.tail5c6a78.ts.net:10000`. Verify DNS, TLS and reachability
from this installation's executor, not just the host. A container does not
inherit host Tailscale identity; never impersonate tailnet identity headers or
disable certificate verification. Other installations use their own gateway.

## Acceptance

Offline fixture, no real key/account/network, using the actual packaged client
and pinned Hermes discovery/handler path:

```bash
python3 tools/check_factory_capykit.py --executor-image driftward/factory-executor:test
python3 tools/check_factory_portable.py --image driftward/factory:test \
  --executor-image driftward/factory-executor:test
```

The first checks granted-action discovery, a GitHub issue lookup, TLS rejection,
grant/key revocation errors (`isError` becomes a Hermes error), bounded responses,
and subprocess credential filtering. The second checks that workers have no
Capykit credential/URL across startup, restart and restore, while Factory stays
dry-run. Fixture responses do not prove live Capykit authorization enforcement.

Live gate requires a newly provisioned dedicated key and an operator-approved
test issue. Through the executor's native MCP tooling:

1. `list_connections`, then `list_connection_actions` for the exact account.
   Require only the approved read-only action; unexpected grants fail the gate.
2. Run `getIssueInformation` once with the deployed schema and approved issue.
   For the reviewed personal GitHub action, inputs are
   `{"repository":{"owner":"APPROVED_ORG","repo":"APPROVED_REPO"},"issue_number":123}`.
   Inspect the bounded result privately; record only pass/fail and non-secret
   identifiers, not issue bodies or provider results in observability.
3. Revoke that grant in Capykit. Rediscover and repeat the same lookup: discovery
   must omit it and the call must fail. Revocation cannot undo an earlier effect.
4. Revoke the key, then check discovery fails authentication. Do not log the key.
5. Confirm worker isolation and Factory's real-execution flags remain unchanged.

No provider/agent generation or Factory workflow activation is required for
this direct read-only tooling check. A successful check does not authorize real
Factory execution. Write actions need a separate effect/idempotency design:
Capykit has no Factory receipt or idempotency-key contract, so never retry an
ambiguous write through this integration.

## Disable

Revoke the dedicated key/grants in Capykit first, then set this MCP entry's
`enabled: false` (or remove only this entry) and restart the next executor
session. Remove the two settings from private `executor.env` when idle. Do not
delete executor sessions/data, replace unrelated MCP settings, or change live
Hatchet workflows to disable tooling.
