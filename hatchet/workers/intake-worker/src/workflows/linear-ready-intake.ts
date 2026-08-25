import {
  BridgeClient,
  HATCHET_EVENTS,
  type BridgeJob,
  type FactoryAttempt,
  type JsonValue
} from '@driftward/hatchet-worker-shared';
import { hatchet } from '../hatchet-client';
import { reserveFactoryIntake } from './linear-ready-factory-intake';
import {
  assertReadyLinearIssueDispatch,
  assertRepoAllowed,
  type LinearReadyIssuePayloadInput
} from './linear-ready-intake-payload';

export type LinearGateResult = {
  name?: string;
  passed?: boolean;
  reasonCode?: string | null;
};

export type LinearReadyIssueInput = LinearReadyIssuePayloadInput & {
  eventKey?: string;
  issueId?: string;
  team?: string;
  teamKey?: string;
  labels?: string[];
  blockedBy?: JsonValue[];
  dispatchEligible?: boolean;
  decision?: string;
  reasonCode?: string;
  deliveryId?: string;
  updatedAt?: string;
  gateResults?: LinearGateResult[];
};

export type LinearReadyIntakeOutput = {
  'reserve-ready-linear-issue-attempt': {
    created: boolean;
    job: BridgeJob;
    attempt: FactoryAttempt;
    idempotencyKey: string;
    repo: string;
    dryRun: boolean;
    acceptedEventEnqueued: boolean;
  };
};

export const linearReadyIntake = hatchet.workflow<LinearReadyIssueInput, LinearReadyIntakeOutput>({
  name: 'intake-linear-ready-dispatch',
  onEvents: [HATCHET_EVENTS.LINEAR_ISSUE_READY_FOR_IMPLEMENTATION]
});

linearReadyIntake.task({
  name: 'reserve-ready-linear-issue-attempt',
  executionTimeout: '30s',
  fn: async (input, ctx) => {
    const route = assertReadyLinearIssueDispatch(input);
    assertRepoAllowed(route.repo);
    const dryRun = intakeDryRun();
    const mode = dryRun ? 'dry-run' : 'real';
    const bridge = new BridgeClient();
    const response = await reserveFactoryIntake(bridge, {
      input,
      route,
      mode,
      hatchetWorkflowRunId: ctx.workflowRunId(),
      hatchetStepRunId: ctx.taskRunExternalId()
    });
    const dryRunResult = response.job.payload.dry_run === true;

    ctx.logger.info('Intake reserved ready Linear issue through factory attempt', {
      identifier: input.identifier,
      sourceEventKey: input.eventKey,
      repo: route.repo,
      jobId: response.job.job_id,
      attemptId: response.attempt.attempt_id,
      created: response.created,
      idempotencyKey: response.job.idempotency_key,
      dryRun: dryRunResult,
      reused: !response.created
    });

    await hatchet.events.push(HATCHET_EVENTS.WORK_ITEM_INTAKE_RECEIVED, {
      identifier: input.identifier,
      sourceEventKey: input.eventKey,
      bridgeJobId: response.job.job_id,
      idempotencyKey: response.job.idempotency_key,
      attemptId: response.attempt.attempt_id,
      repo: route.repo,
      dryRun: dryRunResult,
      acceptedEventEnqueued: response.accepted_event_enqueued === true
    }, {
      additionalMetadata: {
        source: 'intake-worker',
        job_id: response.job.job_id,
        idempotency_key: response.job.idempotency_key
      }
    });

    return {
      created: response.created,
      job: response.job,
      attempt: response.attempt,
      idempotencyKey: response.job.idempotency_key,
      repo: route.repo,
      dryRun: dryRunResult,
      acceptedEventEnqueued: response.accepted_event_enqueued === true
    };
  }
});

function intakeDryRun(): boolean {
  return process.env.HATCHET_INTAKE_DRY_RUN !== 'false';
}
