# Factory feedback and revision loop

Factory keeps one work item connected to its existing draft PR while an operator
supplies approved review or design feedback. Each revision creates a new immutable
execution attempt linked to the prior attempt. Prior attempt status, events,
execution receipts, and verification evidence remain unchanged.

The product benchmark is Warp's separation of a persistent work item, lifecycle
stages, and agent runs, with advisory review feeding revisions before human
handoff. See [How Warp Factories work](https://docs.warp.dev/factories/how-factories-work/)
and [Factory agents](https://docs.warp.dev/factories/factory-agents/) (consulted
October 12, 2026). This is a Factory implementation decision, not a migration to
Warp. A lifecycle stage describes where work is; an attempt kind describes why
execution happened (`initial`, `retry`, `absence_retry`, `repair`, or `revision`);
an agent role and conversation/session are separate concepts. Session continuity
must never overwrite execution history.

## Operator workflow

In the issue console, choose **Revise existing draft PR**, enter actionable
feedback, and optionally attach a design/specification or review reference.
Reviewer and model details are optional. Preview shows the existing PR, branch,
and full commit SHA; confirm approves precisely that observation and feedback.
If the observation is missing or stale, explicitly request **Refresh PR
observation**, wait for the typed inspection worker, and preview again. Page
reads and previews remain read-only.

The root CLI supports the same operation:

```bash
factory revise ENG-X inspect --yes
factory revise ENG-X preview --feedback 'Address the approved review findings' \
  --review-url https://github.com/OWNER/REPO/pull/NUMBER#review \
  --design-url https://example.com/approved-spec
factory revise ENG-X --feedback 'Address the approved review findings' \
  --review-url https://github.com/OWNER/REPO/pull/NUMBER#review \
  --design-url https://example.com/approved-spec \
  --reason 'Approved revision' --request-id UNIQUE-COMMAND-ID \
  --snapshot-digest PREVIEW-DIGEST --yes
```

Use the same feedback and optional references for preview and confirmation.
`--reviewer` and `--review-model` add optional attestation metadata. An exact
replay of the same confirmed command returns the original attempt even after a
lost response or process restart; conflicting feedback, references, actor, or
approval identity fail. Reservation, bridge job, immutable evidence, and accepted
event outbox commit atomically. The inspection request only observes an existing
PR through the existing typed worker; it cannot launch an implementation agent.

## Eligibility and evidence

New revisions require real execution mode, healthy services, an enabled canonical
project route and current verifier policy. The latest trusted issue observation
must be Engineering, unblocked, and in **Ready**, **In Review**, or **Needs
Operator**. These explicit revision gates do not change fresh-intake gates.

The latest real attempt must be terminal (`verified_ready`, `completed`, or
`failed`), with a recorded PR and full commit SHA. Manual control, pending
handoff, active or unresolved execution, import conflicts, legacy imports,
missing artifacts, changed repository/head/branch/base, unknown observations,
closed or merged PRs, and non-draft PRs block approval. A fresh durable trusted
GitHub inspection must identify the same open, unmerged draft PR. Confirmation
rechecks ownership and observation at the SQLite write boundary. The worker
rechecks GitHub before dispatch, and the gateway checks the approved head before
starting a fresh execution. Completed gateway receipts replay without rerunning
the old preflight check or the agent.

The execution reuses `repair_draft_pr` with `trigger=review_feedback`; it does not
fabricate a verifier failure. Its bridge job targets the same PR and branch.
The existing unique attempt reservation branch remains an internal ledger slot,
as for automatic repairs; the execution branch is the bridge job's branch and
is the branch displayed for revision history.

Feedback and references are bounded and sealed with source-attempt lineage,
authenticated operator attribution, and digests. Optional review/design URLs
are references only: Factory does not fetch, snapshot, or verify their contents.
Reviewer/model metadata is an operator attestation, not proof that independent
review ran or passed, and a design reference is not a verified specification
approval gate. The admission manifest records reference and feedback digests;
independent review result remains `unknown`. The revised implementation proceeds
through the existing required validation and verification receipts before
evidence-backed human handoff. Analytics distinguishes revisions from verifier
repairs and fresh retries.

## Current limits

This is the first connected feedback/design-reference → revision → verification
and handoff slice. Planning and independent review happen in operator-facing
interactive sessions; Factory does not automatically orchestrate stage agents,
prove a review's independence, resume an agent conversation, or enforce a
two-review-pass runtime budget. Development review policy is separate from
attempt generations. Factory users can operate through the console and
interactive sessions without chatting directly with Hermes.

Existing agent Git authority remains. The prompt requests normal fast-forward
updates and forbids force-push/history rewriting, but those instructions are not
a technical no-force-push boundary. Head inspections are preflight observations,
not an atomic compare-and-swap at publication; a remote change after inspection
is still possible. A separately reviewed executor/publishing boundary is needed
to enforce that stronger guarantee. This change adds no Git proxy, credential
redesign, new service, merge authority, deployment, or live provider dispatch.

Rollout requires matching bridge/console, executor worker, and gateway versions.
The newly shaped review-feedback context omits legacy verifier-failure fields,
so a legacy strict gateway rejects it before invoking Hermes. Deploying only
the control-plane image does not enable a supported live revision path; update
the applicable bundled executor or separately owned gateway through its reviewed
rollout. Automatic verifier repairs after a revision strip the prior review
attestation and bind their own source attempt and current failed commit before
fresh PR inspection; they remain `repair` attempts, not another review approval.
