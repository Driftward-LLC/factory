# Factory execution-backend evaluation

Status: measurement design; no alternate backend is authorized or deployed

## Current request path

Factory deliberately does not own model-provider credentials. A real draft-PR
attempt currently crosses these boundaries:

```text
Linear webhook
  -> Factory ingress and Hatchet workflow
  -> bridge executor (route, policy, lease, and effect fence)
  -> host-local Hermes gRPC gateway over a Unix socket
  -> driftward-hermes command in the Hermes runtime
  -> Hermes OpenAI Codex provider and its ChatGPT OAuth session
  -> model/tool execution
  -> bounded PR receipt
  -> Factory durable effect commit and verification
```

The deployed Hermes OpenAI Codex provider owns its cached ChatGPT OAuth
credentials and token refresh. The Factory sees only a local gateway result;
it does not see the OAuth exchange, provider request, model stream, or tool
trace. Consequently, a gateway failure, Hermes process failure, OAuth failure,
provider failure, and malformed Hermes result were historically easy to
collapse into one apparent "Hermes failure."

The current Hermes integration calls the ChatGPT Codex backend through
Hermes's provider implementation. That is an implementation detail of the
installed Hermes version, not a Factory-owned or stable API contract. Factory
must not reproduce private endpoint headers or copy OAuth material into its
workers.

OpenAI's supported Codex authentication path can use ChatGPT sign-in and keeps
the cached credentials in the Codex host environment. A future direct-Codex
variant should therefore be another host-local typed gateway that invokes the
official Codex client, not a model credential mounted into a Hatchet worker.
The gateway would return the same bounded Factory receipt and structured
failure envelope as the Hermes adapter.

## What is measured now

Every real execution records an immutable `execution_route_selected` event
before executor preflight. This is the intent-to-treat assignment and includes:

- assignment, experiment, variant, and assignment method;
- intended backend and transport;
- configured provider, model, authentication label, and backend version;
- Factory release, routing-registry version, and verifier-policy digest; and
- whether fallback was allowed.

Today the only executable route is fixed to `factory-default-v1` / `hermes` /
`static`, with fallback disabled. Environment strings cannot relabel that path
as randomized. Deterministic hashing, allocation, and repair-lineage
stickiness must ship as enforced router behavior before an alternate cohort is
considered experimental evidence.

Backend invocation then records invoked, completed, failed, dispatch-failed,
and receipt-commit-failed boundaries. Outer failures distinguish Factory,
gateway transport, gateway handler, Hermes process, Hermes protocol, GitHub,
and verification stages. Bounded transport status, process exit code/signal,
timeout, and output-limit flags are retained; command output, prompts, tokens,
and transcripts are not.

Provider-, OAuth-, model-, and tool-level failure attribution remains
unavailable until the execution backend emits a safe machine-readable
envelope. Reason-code text and configured authentication labels are not used
as a substitute.

## Evaluation design

A Hermes-removal decision requires a contemporaneous direct-Codex cohort. The
reviewed rollout should use the same repository allowlist, routing policy,
verifier policy, effect fence, GitHub identity, prompt contract, and success
definition for both variants.

1. Register `hermes` and `codex_direct` as explicit variants behind the same
   typed host gateway interface.
2. Assign before preflight with a deterministic work-item hash. Keep a work
   item and every retry/repair in its original variant unless the experiment
   protocol explicitly studies fallback.
3. Do not silently fall back between variants. A fallback contaminates both
   reliability cohorts and can create ambiguous effect ownership.
4. Begin with read-only and dry-run contract tests, then a small reviewed real
   canary. Expand only after zero duplicate-effect, identity, credential, and
   receipt-integrity incidents.
5. Compare only mature 24-hour and 7-day outcomes. Report active and unknown
   outcomes separately rather than treating them as failures or successes.
6. Stratify by repository, initial/retry/repair attempt kind, routing-policy
   version, verifier-policy digest, and Factory release. Publish both the raw
   counts and coverage for every rate.

Primary decision measures:

- eligible Ready event to verified-ready conversion;
- verified-ready within 24 hours and within 7 days;
- eligible Ready to first draft PR and to verified-ready latency;
- first-pass CI success and review changes-requested rate;
- classified backend/transport/process/protocol failure rate;
- retries and repairs per ultimately verified or merged work item;
- operator-attention-required rate and actual human rework when available;
- closed-unmerged and merged outcomes at equal maturity; and
- effect-safety incidents, which are release-blocking guardrails rather than
  ordinary performance metrics.

Before assigning real work, the operator should record the minimum mature
sample, acceptable reliability margin, latency threshold, and stop conditions
in the experiment review. The report should not declare a winner from a tiny
or materially unbalanced cohort.

## Decision rules

Hermes can reasonably be called a dominant point of failure only when all of
the following are true:

- route-attribution and terminal-outcome coverage are high enough to exclude
  missing-data bias;
- failures concentrate at the Hermes process/protocol boundary rather than
  Factory preflight, GitHub, verification, or shared provider/auth boundaries;
- the comparable direct-Codex cohort materially improves mature verified
  outcomes or latency under the same policies; and
- the result persists across repositories and releases without worsening
  safety or human rework.

Without that comparison, the architecture can identify Hermes as a meaningful
fragility surface, but not prove that removing it improves the Factory.

## Credential and privacy boundary

- Hatchet workers remain credential-minimal.
- OAuth/API credentials, Codex auth state, Hermes state, GitHub credentials,
  browser state, and transcripts stay outside Factory containers.
- Persist only enums, stable IDs, timestamps, durations, bounded error flags,
  hashes/digests, and aggregate outcomes.
- Never persist prompts, model output, source code, diffs, raw provider errors,
  authorization headers, cookies, or token usage attached to user content.
- Any future inner-backend envelope must be schema-bound, sanitized at the
  producer, versioned, and tested with secret-shaped fixtures before rollout.
