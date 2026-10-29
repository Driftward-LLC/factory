# Execution Dispatch Worker

Submits external-effect jobs to the Hatchet Hermes bridge and returns the
bridge job state. This worker does not call Hermes, GitHub, Linear,
or any model provider directly.

## Workflow

- Workflow: `execution-dispatch-create-draft-pr`
- Task: `submit-create-draft-pr`
- Trigger: `hatchet trigger dry-run --profile prod`

The task builds or accepts an idempotency key, submits `create_draft_pr` to the
bridge, and emits `driftward.hermes.job.accepted` after the bridge returns a
job.

For real PR creation, include bounded execution context in the workflow input:

```json
{
  "workItemId": "ENG-123",
  "repo": "OWNER/REPO",
  "linearIssue": "ENG-123",
  "implementationPrompt": "bounded implementation request",
  "dryRun": false
}
```

The dispatcher only records intent. It does not call Hermes or GitHub.

## Environment

- `HATCHET_CLIENT_TOKEN` and Hatchet connection settings from the Hatchet
  profile or worker env.
- `HATCHET_HERMES_BRIDGE_URL`, default `http://127.0.0.1:19877`.
- `HATCHET_HERMES_BRIDGE_TOKEN`, optional bearer token for the local bridge.

No Hermes or platform credentials belong in this worker.
