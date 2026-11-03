# Installation-local Hermes gateway: opt-in source components

This release includes `compose.local-executor.yml` and
`local-runtime-binding.py` for a separately reviewed installation-local gateway.
They are inactive by default. The image includes the native reviewer and fixed
Composio GitHub receipt reader. Native profile telemetry is provisioned
independently of execution enablement through the reviewed source operation.

The local gateway uses the existing Hermes installation's native executor and
reviewer profiles. Provider and Composio credentials stay in Hermes; the worker
receives a private gateway socket only. Runtime preparation binds the full
container ID, image digest, installation UUID and PID1 incarnation. The operator
observes Docker identity; the container verifies hostname and incarnation and
fails closed after restart or replacement until its metadata is observed again.

Source review provides immutable public Git reads only. Receipts explicitly say
that checks and browser behavior were not executed. Deterministic GitHub reads
use the configured native Composio session and exact configured GitHub principal;
private source is unsupported. Existing legacy GitHub App requirements remain.

These files do not establish an approved activation path. The portable wrapper
continues to enforce dry-run intake and disabled execution. Real admission,
policy enrollment and scoped native cancellation require separate reviewed
configuration and validation. A cancellation marker or a CLI process exit is
not proof of complete cleanup or settled external effects. Do not use this
overlay to bypass those gates or existing production cutover requirements.
