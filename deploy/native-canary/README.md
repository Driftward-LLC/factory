# Isolated native-owner canary

`factory profiles canary native-owner` is a root-only fixed canonical command.
No production route activation, service restart, live provider, GitHub or Linear
call is involved. Do not invoke its backend/container directly.

The existing wrapper returns its sanitized container hostname. The backend
validates the matching running Docker container and immutable image ID, refusing
declared volumes, build hooks and credential-shaped image environment keys.
The disposable container uses that image with **no mounts**, no network, a
read-only root, bounded writable `/tmp`, UID/GID 10000, no capabilities, no new
privileges and CPU/memory/process/file limits. Its entrypoint is `env -i`;
the native subprocess receives a clean environment and fresh HOME/HERMES_HOME,
config/cache/cwd. No real profile checkpoint is touched. Secret material baked
into an image is not audited; the current trusted image remains a prerequisite.
Unsupported source/image/SDK layouts fail closed without installing anything.

Root-owned public assets go over stdin. Six native source hashes must match
pinned 818c13be: this is a measured subset, not whole-revision/dependency proof.
The actual installed SDK uses its own HTTP module's recorded MockTransport.
Native AIAgent, conversation loop, skill helper, dispatcher and terminal remain
real. Registration precedes start authorization; the check requires three
dummy fixed skill markers, the exact terminal-created file, exit zero in the
next actual SDK request, and two recorded requests. Dummy skills do not prove
production instruction parity. Only after result does cancellation occur:
this checks stop wiring, **not in-flight cancellation**, which remains covered
by prior disposable native fixtures. The exact-bound stop stays unknown and
must report dispatch quiescence; owner exit is separate. No whole containment
or external-effect settlement claim follows.

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

Timeout/failure removes only the exact returned created-container ID. An
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
