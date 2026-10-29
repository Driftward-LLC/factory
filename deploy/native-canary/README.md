# Isolated native-owner canary

`factory profiles canary native-owner` is a root-only fixed canonical command.
No production route activation, service restart, live provider, GitHub or Linear
call is involved. Do not invoke its backend/container directly.

The existing wrapper returns its sanitized container hostname. The backend
validates the matching running Docker container and immutable image ID, refusing
all image volume destinations except the measured `/opt/data`, and
credential-shaped image environment keys. Build hooks must have the expected
list-of-strings shape; they do not execute when starting a container.
The disposable container uses that image with fresh bounded tmpfs at `/tmp`
and `/opt/data`, no network, a read-only root, UID/GID 10000, no capabilities, no new
privileges and CPU/memory/process/file limits. Its entrypoint is `env -i`;
the native subprocess receives a clean environment and fresh HOME/HERMES_HOME,
config/cache/cwd. No real profile checkpoint is touched. Secret material baked
into an image is not audited; the current trusted image remains a prerequisite.
Unsupported source/image/SDK layouts fail closed without installing anything.

The accepted image layout allows only the `/opt/data` volume destination.
Verify the actual image's volume declarations and hooks before running. Docker's
[VOLUME documentation](https://docs.docker.com/reference/dockerfile/#volume)
explains why leaving that declaration unoverridden would create an anonymous
volume. The fixed `/opt/data` tmpfs is 16 MiB, mode0700, UID/GID10000 and
noexec/nosuid/nodev; it obscures image data rather than copying it, per the
[tmpfs documentation](https://docs.docker.com/engine/storage/tmpfs/).
[ONBUILD](https://docs.docker.com/reference/dockerfile/#onbuild) applies to
downstream builds, not this image's execution. Inherited healthchecks are
explicitly disabled. Before start, inspect the exact created ID and verify
image/name, created state, nonroot user, disabled healthcheck, network isolation,
read-only root, capability/security settings and fixed tmpfs configuration.
Any bind, volume or unexpected mount refuses before start. These implementation
checks do not themselves establish a completed live canary proof.

Root-owned public assets go over stdin. Six native source hashes must match
pinned 818c13be: this is a measured subset, not whole-revision/dependency proof.
The actual installed SDK uses its own HTTP module's recorded MockTransport.
Native AIAgent, conversation loop, skill helper, dispatcher and terminal remain
real. Registration precedes start authorization; the check requires three
dummy fixed skill markers, the exact terminal-created file, exit zero in the
next actual SDK request, and two recorded requests. Dummy skills do not prove
production instruction parity. The recorded conversation checks stop wiring
after its result. Additional fixed native fixtures then exercise cancellation
while foreground work, ordinary/persistent background work, and a `setsid` child
are alive. The driver requires quiescent dispatch, error-free cleanup, measured
fixture PID exit, a stable duplicate stop receipt, and the supervisor
receipt matching its private durable recovery record. This measures that fixture
tree, not complete escaped-descendant coverage. The exact-bound stop stays unknown and
must report dispatch quiescence; owner exit is separate. No whole containment
or external-effect settlement claim follows.

The same isolated command also checks refusal of a legacy checkpoint, refusal
of profile reuse after successful owner exit, and refusal after killing an exact
new owner before start authorization. That pre-start crash case creates no tool
work. A second case kills the exact registered native owner during foreground, ordinary/persistent background and
escaped-child work. Its live supervisor must drain the adopted descendants and
exit nonzero, preserve the claim and ownership record, and refuse a new invocation.
A concurrent native control invocation and its measured processes must survive.
Restart uses a different job and fresh journal
to prove that the persisted profile claim, not only journal replay protection,
blocks admission. These checks use fresh disposable homes and emit only nine
fixed boolean results under `recovery_checks`; all must pass before the telemetry
relay runs. No profile-unlock, retry, reattachment or activation command is added.
The checks do not prove cleanup after supervisor/host loss or settle external effects.

## Supervisor-loss namespace experiment

After the existing owner/crash checks, the canonical host backend creates two
additional disposable containers using the same verified image and isolation
settings. Each has its own private PID namespace, restart disabled and no extra
init process. The fixed driver verifies the native source subset, prepares a
fresh temporary home, and execs the actual controlled fixture launcher as PID 1.
The supervisor, not a surviving Python driver, is therefore the namespace init.
No Docker socket, capabilities, credentials, host mounts or network are added.

Both target and control must register the exact binding and report live native
foreground, ordinary/persistent background and escaped-child work. The host
captures bounded pidfds from each exact container's Docker process inventory,
checks their PID-namespace membership and required native PIDs, and verifies all
handles are live. Handles are only polled; no host PID is a signaling target.
Creation intent, exact created IDs, readiness and kill intent are persisted in
the private canonical receipt before the corresponding destructive operation.

The host sends SIGKILL through Docker to the exact target container ID. Linux's
[PID-namespace init semantics](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html)
terminate the other namespace members when PID 1 exits. Success requires every
captured target handle to become readable, target container exit 137, and the
independent control container and all its captured processes to remain alive.
Both exact created containers must then be removed. Missing evidence, failed
receipt writes, uncertain creation, or failed removal leaves the canary unknown;
an ambiguous name is never killed or removed. The existing telemetry relay only
runs after this experiment succeeds too.

This proves an isolated mechanism, **not supervisor-loss recovery for the
experimental gateway's shared-runtime `docker exec` route**. That route still
needs a reviewed per-invocation lifecycle boundary. A baseline disposable local
probe at `52990a7` observed the owner, foreground child and detached descendant
surviving supervisor SIGKILL, with no recovery receipt; the test harness reaped
them and the unrelated control survived. Another ordinary watchdog would only
move that dependency. The namespace experiment expects no native recovery receipt
after SIGKILL. Its host evidence remains separate from work completion, safe
profile reuse, host-loss recovery and external-effect settlement. The disposable
profile lives only in tmpfs; no historical profile or claim is reset or migrated.

Actual Owner.observe stages registered/stopping/unknown use an explicit
canary-only callback and private protocol. This proves a **relay**, not production
plugin discovery or direct owner ingest. A separate one-shot process inside
the existing runtime retains only its configured PostHog ingestion key/host,
scrubs provider/App environment credentials, loads a new private reviewed
observer module, validates the complete bounded batch and reuses observe_owner,
its queue and delivery code. No active plugin mutation or registration occurs.
Only US/EU canonical ingestion hosts are supported; no key enters the host or
disposable container. Metadata-only `$ai_span` events carry validation kind
`isolated_native_owner_relay` and the existing deterministic attempt trace ID.
Queue flush is NOT delivery proof: independently query the returned trace,
attempt and three stages in the expected PostHog project. Raw prompts, responses,
commands/output and credentials are never evidence.

Timeout/failure removes only the exact returned created-container IDs, including
its anonymous volumes if a pre-start inspection found unexpected storage. An
ambiguous creation failure never removes a colliding name; its unique
disposable_name remains for reconciliation. Sanitized bounded receipts live at
`<bound installation>/native-canary-<canary UUID>.json`, including asset hashes,
image ID, measured native subset, correlation IDs, cleanup and unknown states.
Raw Docker/native errors are not retained. Preflight refusals create nothing.

## Public helper packaging, after independent review

Copy the exact currently bound helper tree to a new version; preserve its
engineering skills, reviewer assets, gateway asset and other public baseline.
Replace/add only these reviewed candidate files:

- `deploy/factory-profile-runtime.py`
- `deploy/factory-native-canary.py`
- `deploy/native-canary/driver.py`
- `deploy/native-canary/transport.py`
- `deploy/experimental-native-executor/main.py`
- `deploy/experimental-native-executor/owner.py`
- `deploy/factory-observers/posthog/__init__.py` (new private helper asset;
  **never** overwrite the active runtime observer)

Files/directories must be root-owned, non-group/world-writable and nonsymlink.
The bound `deploy/factory-profile-runtime.py` must be executable (0755), as must
the canonical CLI; public Python assets are 0644 and directories 0755. Preflight
refusals return only fixed phase/reason codes, persist a sanitized receipt where
possible, create no container and exit nonzero. Unknown errors expose no text.
Install the reviewed `deploy/factory-cli.py` as the canonical CLI and change only
the protected binding's profile_runtime to the new version. Preserve original
CLI and binding for rollback. No production bundle, other binding field, service,
runtime data or credential changes. Parent/operator provisions after review,
runs the canonical command and independently verifies live PostHog delivery.
