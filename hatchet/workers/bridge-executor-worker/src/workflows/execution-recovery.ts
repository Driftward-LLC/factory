import type { BridgeClient, BridgeJob } from '@driftward/hatchet-worker-shared';
import { inspectHermesInvocation } from '../hermes/hermes-gateway-client';
import { jobMatchesHermesDraftPrReceipt } from '../hermes/create-draft-pr';

/** Reuse the original owner/fence and its durable receipt; never call create here. */
export async function recoverExecutionReceipt(
  bridge: Pick<BridgeClient, 'commitFactoryExecution' | 'getJob' | 'recordExecutionReceiptObservation'>,
  job: BridgeJob,
  cancel: boolean,
  inspect = inspectHermesInvocation,
): Promise<BridgeJob | null> {
  let status;
  try { status = await inspect(job, cancel); }
  catch {
    await bridge.recordExecutionReceiptObservation(job.job_id, {
      state: 'unavailable', cancellation_requested: false, cancellation_acknowledged: false,
    });
    return null;
  }
  await bridge.recordExecutionReceiptObservation(job.job_id, {
    state: status.state, cancellation_requested: status.cancellationRequested, cancellation_acknowledged: status.cancellationAcknowledged,
  });
  if (status.state !== 'completed' || !status.receipt || !status.lease) return null;
  const receipt = status.receipt;
  let committed: BridgeJob;
  try {
    committed = (await bridge.commitFactoryExecution(job.job_id, status.lease, {
      pr_url: receipt.prUrl, commit_sha: receipt.commitSha, branch: receipt.branch, reason_code: `hermes_${receipt.status}`,
      ...(receipt.validation ? { verification_context: receipt.validation } : {}),
    })).job;
  } catch (error) {
    // A concurrent runner/recovery may already have committed this exact receipt.
    committed = await bridge.getJob(job.job_id);
    if (!jobMatchesHermesDraftPrReceipt(committed, receipt)) throw error;
  }
  return committed;
}
