# Experimental native invocation owner

Disabled by default: no production launcher, deployment, gateway, credentials or
service references these files. This is a disposable prototype, **not ready for
activation**. It does not recover ENG-162 or authorize retrying any failed job.

The standalone interpreter handles exactly one request through an inherited
private stdin pipe. A new, exclusive, mode-0700 journal stores the native session,
raw task owner, PID/start ticks, PID namespace, boot identity and random runtime
incarnation before admitting tools. Session rotations are written before their
next tool batch. Existing journal directories are refused; an old record never
authorizes attaching to an old process. Cancellation checks the exact binding,
current process identity and intact durable ownership record before cleanup.
Ownership reads are nonblocking and require a private, bounded regular file;
replaced FIFOs, symlinks and other invalid records are refused before cleanup.

The invocation/epoch/fingerprint fields are **format-validated caller input**.
They are not independently authenticated Factory jobs or leases. The existing
gateway verifier and immutable job fingerprint are not wired to this prototype.
The inherited pipe supplies prototype authority; the journal is evidence only.

Only native local `terminal` and `process_manage` are accepted. Delegation,
browser/MCP tools, remote backends and multiple-tool batches fail closed. The
adapter does not silently remove requested capabilities. Its `AIAgent` subclass
uses the same batch override pattern as the reviewer adapter, but the actual
model/provider initialization and complete AIAgent path are **untested**. They
remain an explicit activation blocker. This is not a supported replacement for
Factory's current complete executor tool surface.

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
effect settlement. Dispatch that fails to quiesce is unknown; there is no crash
recovery, safe resume or periodic retry policy. An interrupted/failed stop-record
write cannot disable already-authorized native cleanup, but is reported as an
evidence failure. The owner cannot observe its own exit; the caller must retain
that separate observation. Old/corrupted records fail closed without cleanup.

An optional caller-supplied observer receives allowlisted PostHog-compatible
metadata. Observer failure cannot change cancellation. The prototype does not
attach a live observer or send fixture events. Existing PostHog queue/export
integration and exact identifier ingestion remain unverified.

## Disposable protocol and tests

The opt-in command is `python main.py --experimental-local-terminal --journal
NEW-DIRECTORY --fixture`, with an explicit disposable `HERMES_HOME`. Its initial
JSON line contains `binding`, `backend: "local"`, and `enabled_tools:
["terminal", "process_manage"]`. `binding` contains canonical
`invocation_id: JOB-UUID:LEASE-UUID:EPOCH`, integer `epoch`, and a 64-character
lowercase hexadecimal `fingerprint`. Later lines are `{"cancel": BINDING}`.
All output is bounded metadata. EOF triggers cleanup using the in-memory exact
binding. Omit `--fixture` only for future model-path testing; that launch also
requires `model`, `provider` and `prompt` fields and remains unverified.
Before any Hermes import, the launcher separates its protocol writer and discards
both native stdout/stderr at the OS descriptor level, including inherited child
output. A provider-free stand-in test exercises import/initialization/error noise
through the actual launcher; this does not verify the real AIAgent model path.

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
and jinja2. No provider request is made. These dependencies are test setup only;
they do not update an installed Hermes runtime or this repository's dependencies.

Native fixtures launch actual `LocalEnvironment.execute` foreground work,
an escaped `setsid` child, and ordinary/persistent `ProcessRegistry.spawn_local`
background work. Three independent interpreters represent the target, another
invocation, and a reviewer control. Tests check wrong-fingerprint refusal, exact
target cleanup, survival of both controls, repeated cancellation, and cancellation
during an already-admitted native spawn. The reviewer control is a disposable
process fixture, not the real reviewer profile or an authenticated model run.

The hard-exit native helper alone was observed to leave the fixture's escaped
child alive. The adapter therefore runs native graceful cleanup first, followed
by the hard-exit spawn fence. This tests the measured fixture tree only, never
complete escaped-descendant coverage. Aggregate stop remains `unknown` even
when every measured fixture process exits. Native dependencies, configuration,
plugins, remote services and production source coverage are not attested by this
experiment. Authentication, gateway wiring, complete capability coverage,
crash/restart behavior and external writes remain activation blockers.
