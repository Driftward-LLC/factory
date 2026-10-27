# Experimental native invocation owner

Disabled by default: the gateway selects this path only with
`DRIFTWARD_HERMES_EXPERIMENTAL_NATIVE_OWNER=1`. This prototype is **not ready for
activation**. No runtime/installer execution, credential change or live run is
part of this change. It does not recover ENG-162 or authorize retrying failed work.

The experimental route now requires `DRIFTWARD_HERMES_PROFILE=factory-native-executor`.
Factory's legacy route refuses that reserved profile, and the native route refuses
the legacy `factory-executor` profile (including an omitted profile). Runtime
identity/model queries and native journals use the selected native profile too.
This is Factory route separation, not an OS boundary against manually launched
Hermes processes with the same UID. Do not point other interpreters at the reserved
home. Provisioning a fresh native profile and its credentials remains a separate
reviewed activation step; this change creates neither profiles nor credentials.

A fresh, single-threaded supervisor forks exactly one native owner before native
imports. The owner handles one request through the inherited private stdin pipe. A new, exclusive, mode-0700 journal stores the native session,
raw task owner, PID/start ticks, PID namespace, boot identity and random runtime
incarnation before admitting tools. Session rotations are written before their
next tool batch. Existing journal directories are refused; an old record never
authorizes attaching to an old process. Cancellation checks the exact binding,
current process identity and intact durable ownership record before cleanup.
Ownership reads are nonblocking and require a private, bounded regular file;
replaced FIFOs, symlinks and other invalid records are refused before cleanup.

Through the gateway, the binding is derived only after its existing authenticated
handler reserves the immutable job fingerprint and full lease in InvocationJournal.
No caller fingerprint is accepted. Recovery reads an authenticated, exact-job,
fingerprint-verified snapshot and submits that complete lease as a compare-and-swap
claim; stale owner/epoch/fence fails before cancel-marker mutation. This preserves
the existing transport/private-journal trust boundary, **not independent current
bridge-lease attestation**. Standalone fixture bindings remain format-validated
caller input. Disk evidence never authorizes attaching to an old process.

The owner runs inside the existing Driftward Hermes runtime/user and reserved native profile through
the same Docker Compose arguments as identity checks. A reviewer-style fixed
bootstrap contains checked-in public Python assets; job data travels through
private stdin and never becomes Python source or arbitrary executable argv. The
existing installer copies the host assets but is not run here. The existing
artifact/job gates, identity preflight, prompt construction, model selection and
Factory result normalizer remain in place. Only an ephemeral, bounded,
policy-filtered result candidate crosses the private protocol; raw responses are
not persisted in the invocation or stop journal. The default executor is unchanged.

Only native local `terminal` and `process_manage` are accepted. Delegation,
browser/MCP tools, remote backends and multiple-tool batches fail closed. The
adapter does not silently remove requested capabilities: the profile must already
explicitly select `platform_toolsets.cli: [terminal]` and
`tools.tool_search.enabled: 'off'`. All three fixed engineering skills must load
through the pinned native skill-preload helper, or initialization refuses before
provider/tool execution. Native context/SOUL loading is enabled at the runtime cwd.
The actual `AIAgent` path is now tested with a recorded SDK HTTP transport and real
native terminal dispatch; full production provider/configuration parity is not
attested. A broader profile, including an observer-added `factory_github` tool,
is refused. This is not a replacement for Factory's complete executor tool surface.

The controlled protocol requires registration followed by host start authorization.
Cancellation before/during registration withholds that frame; writing launch and
cancel back-to-back is not treated as a proven dispatch barrier. Cooperating owners
also hold a profile lock before native initialization/effects. Disposable tests
show why: separate legacy interpreters sharing HERMES_HOME overwrite each other's
process checkpoint even while their unrelated processes stay alive. Experimental
owners are serialized; the lock cannot coordinate nonparticipating legacy agents.
Admission additionally refuses any existing `processes.json`, even empty, and
exclusively creates a private `factory-native-owner.claim/` directory before
Hermes imports. The directory and bounded exact binding are persisted with fsync.
An incomplete claim also blocks reuse. Symlinked or writable-by-others profile
homes are refused. There is no checkpoint repair, private-path override or
credential copying.

The claim is deliberately retained after every exit, including a successful
conversation: containment remains unknown. A new invocation or journal, released
flock, missing PID, or empty checkpoint cannot reopen that profile. This makes
the prototype **single-use per fresh profile until reviewed recovery exists**.
Do not delete claims or checkpoints to bypass admission. The change establishes
restart refusal and bounded cleanup after the native owner exits while its
supervisor survives; it adds no safe reattachment or reset/reuse command. Existing host helpers and profiles are not migrated.

Cancellation fences new batch admission, hard-interrupts the attached native
agent, runs native graceful foreground cleanup followed by the native hard-exit
spawn fence, waits up to three seconds for admitted dispatch to quiesce, and
explicitly kills background sessions selected by their exact raw owner. The
explicit source includes `persist_on_release` work. The process-global native
foreground helper is used only in this dedicated standalone interpreter, never
in a shared Hermes agent process. The helper module is not a general shared-host
cancellation API.

Stop evidence persists requested/stopping timestamps and an `unknown` result,
separate from completion. It retains ownership digest, native result metadata,
quiescence, errors and explicit unmeasured coverage. No natural-language answer,
kill count, missing PID or receipt authorizes completion, containment or external
effect settlement. Dispatch that fails to quiesce is unknown; crash recovery
retains the admission barrier and requires operator review, without safe resume
or periodic retry. An interrupted/failed stop-record
write cannot disable already-authorized native cleanup, but is reported as an
evidence failure. The owner cannot observe its own exit; the caller must retain
that separate observation. Docker-client exit alone is not owner-exit proof: a
validated owner-exit frame, quiescent dispatch, empty cleanup errors and successful
remote command exit, plus the supervisor's error-free descendant-drain receipt,
are all required before the controller accepts a candidate. Old/corrupted records
fail closed without authorizing attachment or PID-based recovery.

## Recovery while the supervisor survives

Linux `PR_SET_CHILD_SUBREAPER` makes orphaned descendants children of this dedicated
parent, including `setsid` and double-fork descendants. The supervisor launches no
unrelated work, keeps default SIGCHLD handling and is the only reaper. It checks
required subreaper/pidfd/waitid support before forking, and persists
`factory-native-owner.claim/supervisor.json` before allowing native initialization.
Missing kernel support or a failed initial write refuses before child effects.

After observing its owner exit with `waitpid`, the parent repeatedly discovers only
its kernel children, opens each pidfd, checks current-child authority through
`waitid(P_PIDFD, WNOWAIT)`, signals SIGKILL through that handle, and reaps. Repeated
adoption handles additional generations. Only kernel `ECHILD` establishes a drained
local descendant tree; an empty process-list snapshot does not. No persisted PID,
checkpoint, process name, group ID, or system-wide process search authorizes a kill.
The loop is bounded to five seconds and 4096 signaling attempts. Identity, signal,
wait, bound and journal failures remain explicit errors and prevent success.

The parent writes a separate private `recovery.json` and emits the bounded
`owner_reaped` frame after cleanup. Transport loss does not disable cleanup or the
private write. The gateway retains only the exit code, drain boolean and fixed
error codes in its unknown stop evidence; old stop records remain readable. A
successful drain after owner SIGKILL is still a nonzero invocation exit, never a
completed work receipt. The claim and original ownership record remain intact.

This is cooperative process recovery, not an OS security boundary: same-UID code
can interfere with the supervisor. Supervisor death, host/runtime loss, kernel
cleanup failures, processes outside its ancestry, remote resources and external
effects remain unproven. Nothing resets a profile, migrates old claims, reattaches
to historical work or authorizes retry. Profiles remain single-use even after an
error-free local drain. A stronger lifecycle boundary and external-effect review
are still needed before safe reuse or activation.

Linux semantics: [subreapers](https://man7.org/linux/man-pages/man2/PR_SET_CHILD_SUBREAPER.2const.html),
[pidfds](https://man7.org/linux/man-pages/man2/pidfd_open.2.html), and
[waitid](https://man7.org/linux/man-pages/man2/waitid.2.html).

The adapter may attach the already-loaded observer's public `observe_owner` sink.
It reuses that observer's queue and deterministic attempt/job trace to emit bounded
AI spans. Native generation/tool hooks remain owned by the existing plugin. There
is no second exporter; absent/older plugins and sink failures do not affect stop
semantics. Tests mock delivery; no fixture event is sent to live PostHog and exact
identifier ingestion remains unverified.

## Disposable protocol and tests

The opt-in command is `python main.py --experimental-local-terminal --journal
NEW-DIRECTORY --fixture`, with an explicit disposable `HERMES_HOME`. Its initial
JSON line contains `binding`, `backend: "local"`, and `enabled_tools:
["terminal", "process_manage"]`. `binding` contains canonical
`invocation_id: JOB-UUID:LEASE-UUID:EPOCH`, integer `epoch`, and a 64-character
lowercase hexadecimal `fingerprint`. Later lines are `{"cancel": BINDING}`.
All output is bounded metadata or a filtered result candidate. EOF requests cleanup
using the in-memory exact binding. Non-fixture launch additionally requires model,
provider, prompt and receipt policy. Controlled launch requires
`{"start": BINDING}` after registration; the gateway supplies that authorization.
Before any Hermes import, the launcher separates its protocol writer and discards
both native stdout/stderr at the OS descriptor level, including inherited child
output. A provider-free stand-in test exercises import/initialization/error noise
through the actual launcher. A separate recorded-transport check uses real AIAgent.

Unit checks run in the ordinary tools suite. Native checks are explicit opt-in:

```sh
FACTORY_TEST_HERMES_SOURCE=/path/to/disposable/hermes-agent \
FACTORY_TEST_HERMES_PYTHON=/path/to/disposable/venv/bin/python \
python3 -m unittest discover -s tools -p 'test_hermes_native_owner.py' -v
```

The native checkout must be clean at
`818c13be1dc4fd28987e1e881a9408224afd4535` (`v0.21.6`). Tests import its full native
modules, not copied methods, stubs or synthetic termination callbacks. Isolated
interpreter dependencies used for this run: ruamel.yaml, PyYAML, python-dotenv,
requests, httpx, tenacity, rich, platformdirs, pydantic, psutil, openai, anthropic
jinja2 and snowballstemmer. The recorded fixture uses Python 3.12, OpenAI SDK
3.37.0, httpx2 2.16.0 and snowballstemmer 3.1.1; this is a separate test dependency
set, not attestation of the installed runtime. All socket connections are forbidden
and no credentials/live provider request are used. These dependencies are test setup
only and do not update the runtime or repository dependencies.

Native fixtures launch actual `LocalEnvironment.execute` foreground work,
an escaped `setsid` child, and ordinary/persistent `ProcessRegistry.spawn_local`
background work. Three independent interpreters represent the target, another
invocation sharing its profile, and a reviewer control. Tests check wrong-fingerprint refusal, exact
target cleanup, survival of both controls, repeated cancellation, and cancellation
during an already-admitted native spawn. The reviewer control is a disposable
process fixture, not the real reviewer profile or an authenticated model run.
The owner checks include actual AIAgent initialization, SDK streaming parsing,
native terminal dispatch, exact filesystem marker and exit-zero tool-result proof,
all three fixed skill markers in the provider request, missing-skill refusal,
same-profile admission, durable/incomplete claims, and the measured legacy checkpoint conflict. The SDK HTTP
transport is replaced; AIAgent, conversation loop, dispatcher and terminal are not.
The genuine AIAgent fixture uses the gateway's controlled registration/start
handshake, and verifies no effect or provider proof exists before authorization.
Worker tests separately verify gateway auth, exact lease claims, start barriers,
completion races, timeout/crash/transport-loss evidence, missing/malformed supervisor
receipts and result normalization. Supervisor tests separately exercise real nested
and double-fork descendants, sibling survival, capability refusal, identity/signal/
wait/deadline failures, and journal failures before admission and after cleanup.
Their Docker transport is a fixture, not a real container execution.

The hard-exit native helper alone was observed to leave the fixture's escaped
child alive. The adapter therefore runs native graceful cleanup first, followed
by the hard-exit spawn fence. This tests the measured fixture tree only, never
complete escaped-descendant coverage. Aggregate stop remains `unknown` even
when every measured fixture process exits. Native dependencies, configuration,
plugins, remote services and production source coverage are not attested by this
experiment. Full capability/configuration/provider parity, nonparticipating legacy
coexistence, installed runtime verification, supervisor/host-loss recovery, safe
profile reuse, production telemetry ingestion, remote resources and external settlement remain activation blockers.
