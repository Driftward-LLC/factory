import {
  BridgeClient,
  type BridgeJob,
  type FactoryAttempt
} from '@driftward/hatchet-worker-shared';
import { type ResolvedLinearProjectRoute } from './linear-project-routing';
import {
  assertExistingJobRouting,
  buildBridgePayload,
  type LinearReadyIssuePayloadInput
} from './linear-ready-intake-payload';

export type FactoryIntakeInput = LinearReadyIssuePayloadInput & {
  eventKey?: string;
  deliveryId?: string;
};

export async function reserveFactoryIntake(
  bridge: Pick<BridgeClient, 'reserveFactoryIntake'>,
  input: {
    input: FactoryIntakeInput;
    route: ResolvedLinearProjectRoute;
    mode: 'dry-run' | 'real';
    hatchetWorkflowRunId: string;
    hatchetStepRunId: string;
  }
): Promise<{ created: boolean; accepted_event_enqueued?: boolean; attempt: FactoryAttempt; job: BridgeJob }> {
  const response = await bridge.reserveFactoryIntake({
    work_item_key: requiredIdentifier(input.input.identifier),
    mode: input.mode,
    request_id: factoryIntakeRequestId(input.input, input.hatchetWorkflowRunId, input.hatchetStepRunId),
    reason: 'Linear Ready intake reserved a factory attempt',
    hatchet_workflow_run_id: input.hatchetWorkflowRunId,
    hatchet_step_run_id: input.hatchetStepRunId,
    payload: buildBridgePayload(input.input, input.route, input.mode === 'dry-run')
  });
  assertExistingJobRouting(response.job, input.route);
  return response;
}

function requiredIdentifier(identifier?: string): string {
  if (!identifier) {
    throw new Error('identifier is required');
  }
  return identifier;
}

function factoryIntakeRequestId(input: FactoryIntakeInput, workflowRunId: string, stepRunId: string): string {
  return input.eventKey || input.deliveryId || `${workflowRunId}:${stepRunId}`;
}
