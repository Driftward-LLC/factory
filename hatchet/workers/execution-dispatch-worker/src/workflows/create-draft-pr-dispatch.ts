import {
  BridgeClient,
  HATCHET_EVENTS,
  buildIdempotencyKey,
  type BridgeJob
} from '@driftward/hatchet-worker-shared';
import { hatchet } from '../hatchet-client';

export type CreateDraftPrDispatchInput = {
  requestId?: string;
  idempotencyKey?: string;
  workItemId: string;
  repo: string;
  linearIssue?: string;
  expectedHeadSha?: string;
  implementationPrompt?: string;
  instructions?: string;
  repoPath?: string;
  title?: string;
  body?: string;
  dryRun?: boolean;
};

export type CreateDraftPrDispatchOutput = {
  'submit-create-draft-pr': {
    created: boolean;
    job: BridgeJob;
  };
};

export const createDraftPrDispatch = hatchet.workflow<CreateDraftPrDispatchInput, CreateDraftPrDispatchOutput>({
  name: 'execution-dispatch-create-draft-pr'
});

createDraftPrDispatch.task({
  name: 'submit-create-draft-pr',
  executionTimeout: '30s',
  fn: async (input, ctx) => {
    if (!input.workItemId || !input.repo) {
      throw new Error('workItemId and repo are required');
    }

    const idempotencyKey = input.idempotencyKey || buildIdempotencyKey({
      requestId: input.requestId,
      workItemId: input.workItemId,
      action: 'create_draft_pr',
      expectedHeadSha: input.expectedHeadSha
    });

    const bridge = new BridgeClient();
    const response = await bridge.submitJob({
      idempotency_key: idempotencyKey,
      requested_action: 'create_draft_pr',
      hatchet_workflow_run_id: ctx.workflowRunId(),
      hatchet_step_run_id: ctx.taskRunExternalId(),
      payload: {
        work_item_id: input.workItemId,
        repo: input.repo,
        linear_issue: input.linearIssue || null,
        expected_head_sha: input.expectedHeadSha || null,
        implementation_prompt: input.implementationPrompt || null,
        instructions: input.instructions || null,
        repo_path: input.repoPath || null,
        title: input.title || null,
        body: input.body || null,
        dry_run: Boolean(input.dryRun)
      }
    });

    ctx.logger.info('Bridge accepted create_draft_pr job', {
      jobId: response.job.job_id,
      created: response.created,
      idempotencyKey
    });

    await hatchet.events.push(HATCHET_EVENTS.HERMES_JOB_ACCEPTED, {
      jobId: response.job.job_id,
      idempotencyKey,
      created: response.created,
      requestedAction: response.job.requested_action,
      state: response.job.state
    }, {
      additionalMetadata: {
        source: 'execution-dispatch-worker',
        job_id: response.job.job_id,
        idempotency_key: idempotencyKey
      }
    });

    return response;
  }
});
