# Bounded independent review

Status: implemented behind a disabled policy; no live enrollment, execution,
merge, deployment, or credential provisioning is authorized by this change.

Factory separates deterministic checks from independent review. The persisted
`verified_ready` attempt and `ready_for_human_review` job still mean checks
passed. The independent review ledger separately records pending, passed,
changes requested, or needs operator. A passed review grants no merge/deploy
authority and is valid only for the recorded full commit SHA.

The checked-in `hatchet/workers/independent-review-policy.json` defaults to
`enabled: false`, `automatic_repairs_allowed: false`, no repositories, and an
immutable maximum of two review passes. Policy lives with the deployed source,
not in model prompts or worker payloads. Only newly admitted real intake under
an enabled repository policy receives a digest-bound enrollment. Existing
verified jobs do not become enrolled when the gate is enabled. A later policy
change does not silently adopt old work.

## State and authority

Enrolled implementation uses GPT-6.1 Sol; the independent reviewer uses
GPT-6 Astra. The original task requirements, expected base ref, exact checked
head, generator session, and policy digest bind the typed review request. The
reviewer's source loader must pin the base commit in its immutable source
bundle. Requirements and findings are untrusted content, never commands or
authority.

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

Before considering activation, approve a deployment of schema version 9 with
the existing quiesced migration/backup procedure. The new review table and
existing accepted-event outboxes are durable application state and must be
preserved by migration and rollback inventory.

Provision and independently validate an isolated reviewer backend through the
existing typed gateway. The generator's shared Hermes runtime is not a review
sandbox. The reviewer needs a separate session, checkout, browser state and
provider identity; it must have no GitHub push/merge credentials or permissions,
host root data, generator session state, or worker credentials. Missing backend
capability remains a visible needs-operator result. Follow the review backend's
provisioning instructions and verify isolation with synthetic fixtures before
any real work is enabled.

Repository enrollment and automatic repair require a separately reviewed
source policy change. Enabling review alone does not enable repair. No UI,
review receipt, PR check, or model can flip either policy flag. Console views
are read-only projections and offer no repair, merge, or deploy approval.
