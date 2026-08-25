import { BridgeClient, HATCHET_EVENTS, type JsonObject } from '@driftward/hatchet-worker-shared';
import { hatchet } from '../hatchet-client';
import {
  NonRetryableBridgeExecutionError,
  hermesReceiptEventScope,
  runHermesCreateDraftPr,
  sanitizeText,
  type FactoryExecutionLease
} from './create-draft-pr';

async function main(): Promise<void> {
  const jobId = process.argv[2];
  if (!jobId) throw new Error('job id is required');
  const lease = parseLease(process.env.HATCHET_FACTORY_EXECUTION_LEASE);
  const bridge = new BridgeClient();
  const job = await bridge.getJob(jobId);

  try {
    const result = await runHermesCreateDraftPr(job);
    const committed = await bridge.commitFactoryExecution(job.job_id, lease, {
      commit_sha: result.commitSha,
      pr_url: result.prUrl,
      hermes_session_id: result.hermesSessionId,
      reason_code: `hermes_${result.status}`,
      branch: result.branch,
      summary: result.summary,
    });
    const pending = result.validation && typeof result.validation === 'object' && !Array.isArray(result.validation)
      ? await bridge.transitionJob(committed.job.job_id, {
        state: 'verification_pending',
        details: {
          reason_code: 'draft_pr_verification_pending',
          verification_context: result.validation as JsonObject
        }
      })
      : committed;
    await hatchet.events.push(HATCHET_EVENTS.HERMES_JOB_EFFECT_COMMITTED, {
      jobId: pending.job.job_id,
      idempotencyKey: pending.job.idempotency_key,
      prUrl: pending.job.pr_url,
      prNumber: result.prNumber,
      branch: pending.job.branch,
      commitSha: pending.job.commit_sha,
      hermesSessionId: pending.job.hermes_session_id,
      status: result.status,
      summary: result.summary,
      validation: result.validation,
      dispatchId: process.env.HATCHET_HERMES_RECEIPT_DISPATCH_ID || null,
      dryRun: false
    }, {
      scope: hermesReceiptEventScope(pending.job),
      additionalMetadata: {
        source: 'bridge-executor-hermes-receipt-runner',
        job_id: pending.job.job_id
      }
    });
    if (pending.job.state === 'verification_pending') {
      await hatchet.events.push(HATCHET_EVENTS.HERMES_JOB_VERIFICATION_PENDING, {
        jobId: pending.job.job_id,
        idempotencyKey: pending.job.idempotency_key,
        prUrl: pending.job.pr_url,
        dryRun: false
      }, { additionalMetadata: { source: 'bridge-executor-hermes-receipt-runner', job_id: pending.job.job_id } });
    }
    await emitPrCreated(pending.job, result.prNumber);
  } catch (error) {
    await settleUnknown(bridge, job.job_id, lease);
    const reasonCode = error instanceof NonRetryableBridgeExecutionError
      ? error.reasonCode
      : 'factory_execution_recovery_required';
    await hatchet.events.push(HATCHET_EVENTS.HERMES_JOB_FAILED, {
      jobId: job.job_id,
      idempotencyKey: job.idempotency_key,
      reasonCode,
      error: sanitizeText(error instanceof Error ? error.message : String(error)),
      dispatchId: process.env.HATCHET_HERMES_RECEIPT_DISPATCH_ID || null,
      dryRun: false
    }, {
      scope: hermesReceiptEventScope(job),
      additionalMetadata: {
        source: 'bridge-executor-hermes-receipt-runner',
        job_id: job.job_id
      }
    });
    process.exitCode = 1;
  }
}

async function emitPrCreated(job: { job_id: string; pr_url: string | null; payload: JsonObject }, prNumber: number): Promise<void> {
  await hatchet.events.push(HATCHET_EVENTS.GITHUB_PR_CREATED, {
    repo: String(job.payload.repo || ''),
    prNumber,
    prUrl: job.pr_url,
    bridgeJobId: job.job_id,
    dryRun: false
  }, {
    additionalMetadata: {
      source: 'bridge-executor-hermes-receipt-runner',
      job_id: job.job_id
    }
  });
}

async function settleUnknown(bridge: BridgeClient, jobId: string, lease: FactoryExecutionLease): Promise<void> {
  try {
    await bridge.settleFactoryExecution(jobId, lease, false);
  } catch (error) {
    console.error('failed to settle factory execution after Hermes receipt failure', sanitizeText(error instanceof Error ? error.message : String(error)));
  }
}

function parseLease(raw: string | undefined): FactoryExecutionLease {
  if (!raw) throw new Error('HATCHET_FACTORY_EXECUTION_LEASE is required');
  const value = JSON.parse(raw) as Partial<FactoryExecutionLease>;
  if (typeof value.lease_id !== 'string' || typeof value.owner !== 'string' || typeof value.epoch !== 'number' || typeof value.fence_token !== 'string') {
    throw new Error('HATCHET_FACTORY_EXECUTION_LEASE is malformed');
  }
  return {
    lease_id: value.lease_id,
    owner: value.owner,
    epoch: value.epoch,
    fence_token: value.fence_token
  };
}

if (require.main === module) {
  main().catch((error) => {
    console.error(sanitizeText(error instanceof Error ? error.message : String(error)));
    process.exit(1);
  });
}
