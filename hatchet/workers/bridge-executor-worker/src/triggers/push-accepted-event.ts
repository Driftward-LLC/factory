import { HATCHET_EVENTS } from '@driftward/hatchet-worker-shared';
import { hatchet } from '../hatchet-client';

async function main() {
  const jobId = process.env.BRIDGE_JOB_ID;
  const idempotencyKey = process.env.BRIDGE_IDEMPOTENCY_KEY;

  if (!jobId && !idempotencyKey) {
    throw new Error('Set BRIDGE_JOB_ID or BRIDGE_IDEMPOTENCY_KEY');
  }

  const event = await hatchet.events.push(HATCHET_EVENTS.HERMES_JOB_ACCEPTED, {
    jobId,
    idempotencyKey
  }, {
    additionalMetadata: {
      source: 'bridge-executor-worker-trigger',
      job_id: jobId || '',
      idempotency_key: idempotencyKey || ''
    }
  });

  console.log(JSON.stringify({
    eventKey: HATCHET_EVENTS.HERMES_JOB_ACCEPTED,
    eventId: event.eventId,
    jobId,
    idempotencyKey
  }, null, 2));
}

if (require.main === module) {
  main().catch((error) => {
    console.error(error);
    process.exit(1);
  });
}
