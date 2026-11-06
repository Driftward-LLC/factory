# Bounded independent review

Status: supported for newly admitted real work covered by the deployed
repository policy; automatic repair remains disabled by default. Runtime
activation requires a compatible deployment and provisioned native backend.

Factory separates deterministic checks from independent review. The persisted
`verified_ready` attempt and `ready_for_human_review` job still mean checks
passed. The independent review ledger separately records pending, passed,
changes requested, or needs operator. A passed review grants no merge/deploy
authority and is valid only for the recorded full commit SHA.

The checked-in `hatchet/workers/independent-review-policy.json` selects
`enabled: true`, `automatic_repairs_allowed: false`, a fixed repository allowlist,
and an immutable maximum of two review passes. Review that allowlist before
configuring an installation; it is not an automatic grant to arbitrary repositories.
Policy lives with the deployed source,
not in model prompts or worker payloads. Normal intake, return-to-Factory retries, and absence retries share one trusted admission decision. Only newly admitted real work under
an enabled repository policy receives a digest-bound enrollment. Existing
verified jobs do not become enrolled when the gate is enabled. A later policy
change does not silently adopt old work.

## State and authority

Enrolled implementation uses GPT-6.1 Sol; the independent reviewer uses
GPT-6 Astra. The original task requirements, expected base ref, exact checked
head, Factory generator execution, and policy digest bind the typed review request. New
requests use `generator_execution_id`, the deterministic
`factory-execution:<source-attempt-uuid>:<source-job-uuid>` reference derived from
the verified real Factory source. It identifies Factory execution, not a Hermes
session. Existing requests with `generator_session_id` remain readable; exactly
one generator identity field is accepted. New bounded execution receipts still
exclude Hermes session material, and telemetry is not execution authority. The
reviewer's source loader must pin the base commit in its immutable source
bundle. Requirements and findings are untrusted content, never commands or
authority.

Reviewer independence comes from the gateway's fresh native reviewer UUID and
profile, separate authentication, empty session database, and isolated source and
browser capabilities. Legacy session identity comparisons remain enforced for
legacy requests; a Factory execution reference is not a substitute Hermes session.

This change applies only when reserving a new review. It does not rewrite source
jobs, stored requests/results, or existing `needs_operator` reservations, including
preflight failures caused by a missing legacy session ID. Those retain their
original shared PR pass budget. The narrow `review-recovery` command can deliver an eligible legacy unstarted reservation without allocating another pass. Other stopped reviews require an operator; deployment alone does not recover them.

The bridge transaction reserves the pass and its existing accepted-event
outbox before dispatch. Budget identity is the canonical repository and PR
URL, shared across commits, repair attempts, retries, and service restarts.
Unique constraints and protected identities prevent automatic budget resets.
Duplicate delivery reuses the same reservation. A claimed invocation cannot
be claimed again; missing, malformed, unsupported, timed-out, or ambiguous
results require an operator. There is no model fallback and no third review.
Before invoking the model, the native gateway checks the exact active claimed
reservation against the authenticated bridge authorization endpoint. A forged
job envelope cannot manufacture an extra review pass. The gateway's bridge and
inspection credentials stay outside both reviewer containers. Recovery may
return an already retained receipt but cannot launch another model call.

The native review operation has a 15-minute command budget plus bounded
authorization and cleanup; the gateway call allows 17 minutes and the existing
workflow task allows 25 minutes. Launch
authorization requires a fresh inspection. Completion authorization preserves
the policy, manual-control, latest-attempt, and exact-artifact checks while
allowing the launch observation to age naturally during review; it grants no
pass. The bridge still obtains a fresh final typed inspection before settling
the retained receipt. An unchanged repair head requires an operator and does
not allocate or execute another review pass.

A completed reviewer receipt is retained while the existing typed inspection
outbox obtains a fresh final observation. The reviewer is not rerun while
waiting. A changed, closed, merged, or non-draft artifact fails closed. The
review result cannot overwrite implementation verification or work evidence.

Findings after pass one may reserve exactly one repair only when automatic
repair is explicitly enabled in the same checked-in policy. The distinct
`independent_review_policy_v1` authority uses `review_repair.py`'s current
Linear eligibility, route/verifier policy, issue control, latest settled
attempt, typed fresh draft inspection, exact head/branch/PR fences, and existing
repair attempt/outbox. It does not impersonate root or reuse a root approval.
The effect grant rechecks the policy and its linked reservation. The worker
and credential-bearing execution process recheck the artifact before repair.
The repair performs the normal checks and consumes pass two when green.
Automatic verification repairs are disabled for this repair. Remaining
findings after pass two retain changes-requested evidence with an operator
required budget reason. Manual operator decisions never reset this budget.

## Activation prerequisites

Deploy the compatible typed gateway parser together with the Factory image before
admitting the new request variant. An older host gateway rejects it; updating only
the container is not a compatible rollout. Schema 11 adds an append-only existing-artifact enrollment ledger; it does not backfill historical admissions.

Before considering activation, approve a deployment of schema version 11 with
the existing quiesced migration/backup procedure. The new review table and
existing accepted-event outboxes are durable application state and must be
preserved by migration and rollback inventory.

Provision and independently validate an isolated reviewer backend through the
existing typed gateway. The native execution profile separates state and fixes
the model/tool boundary; the offline source container isolates tests/browser
from provider/platform credentials. The reviewer uses a separate native ChatGPT
sign-in under its dedicated auth root, without cloning tokens or generator
context, and has no GitHub write tools. Missing dedicated login fails closed;
generator/root and external CLI credentials are not fallback identities.
Follow [the native backend runbook](../deploy/INDEPENDENT-REVIEW.md). Missing backend
capability remains a visible needs-operator result. Follow the review backend's
provisioning instructions and verify isolation with synthetic fixtures before
any real work is enabled.

Repository enrollment is limited to new admissions under the deployed policy
digest; historical work is never backfilled. Automatic repair requires another
reviewed source policy change. Enabling review alone does not enable repair. No UI,
review receipt, PR check, or model can flip either policy flag. Console views
are read-only projections and offer no repair, merge, or deploy approval.


## Existing verified drafts

Missing historical enrollment appears as **Unknown**, regardless of today's
policy. New recorded exclusions appear as **Not required** with their reason.
Enrolled work without a reservation appears as **Awaiting review**; a changed
policy requires an operator. Verification alone never means review passed.

An authenticated root operator can explicitly enroll a latest verified real
attempt under the current enabled repository policy. This uses the original
requirements and exact currently inspected open draft head, creates no new
implementation attempt, and runs the existing independent reviewer. Any prior
PR review budget, conflicting work, hold, stale inspection, changed policy or
changed head blocks enrollment. Original payloads and admission evidence remain
unchanged. The separate append-only record binds source attempt/job, artifact,
policy, zero previously consumed passes, actor, reason and request. Apply and
review reservation share one transaction; repeated identical requests reuse it.
Repairs inherit effective enrollment and the same two-pass PR budget.

For a portable image installation, the installed wrapper supports:

```sh
factory review-enrollment ENG-X inspect --source-attempt ATTEMPT-ID --yes
factory review-enrollment ENG-X preview --source-attempt ATTEMPT-ID --repo OWNER/REPO --pr URL --branch BRANCH --reviewed-head FULL-SHA --reason 'review existing verified draft'
factory review-enrollment ENG-X confirm --source-attempt ATTEMPT-ID --repo OWNER/REPO --pr URL --branch BRANCH --reviewed-head FULL-SHA --reason 'review existing verified draft' --request-id UNIQUE-ID --snapshot-digest PREVIEW-DIGEST --yes
```

Wait for the typed inspection requested by `inspect` before previewing. The
wrapper invokes the operator role using its existing private Unix socket and
bearer credential; workers gain no credential or new mount. A source checkout's
operator CLI exposes the same command. Review receipts still require independent
isolation and a fresh final exact-head inspection. These offline fixture tests
do not prove a native reviewer was invoked; published-image credentialed canary
acceptance remains a release gate. Follow the
[release canary procedure](factory-release-runbook.md#independent-review-release-acceptance)
using either an isolated installation or an explicitly approved, contained
maintenance canary in the existing installation. Existing supported authenticated
executor/reviewer profiles may be reused with separate identities preserved;
never clone tokens. The retry fix requires a genuine operator-approved retry
through enrollment, verification, native review, a real receipt and exact-head
console evidence. Explicit enrollment of an existing verified draft alone does
not satisfy that retry acceptance gate.

Schema 10 installations must stop writers, retain a verified cold backup and
run the existing explicit bridge `init` migration before restarting schema 11
services. Repeated migration is idempotent and historical rows stay unchanged.
An old image refuses schema 11; rollback requires restoring the pre-upgrade cold
backup with its matching image, never downgrading metadata or deleting audit rows.
The separate naming migration still requires its documented legacy schema 10
source; it is followed by the target image's explicit schema upgrade.
