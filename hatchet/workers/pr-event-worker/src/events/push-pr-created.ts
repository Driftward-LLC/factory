import { HATCHET_EVENTS } from '@factory/hatchet-worker-shared';
import { hatchet } from '../hatchet-client';

async function main() {
  const repo = process.env.PR_REPO;
  if (!repo || !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo)) throw new Error('PR_REPO must name a repository');
  const event = await hatchet.events.push(HATCHET_EVENTS.GITHUB_PR_CREATED, {
    repo,
    prNumber: Number(process.env.PR_NUMBER || 0),
    prUrl: process.env.PR_URL || 'dry-run://github/pr/synthetic',
    title: process.env.PR_TITLE || 'Synthetic PR event',
    state: process.env.PR_STATE || 'open',
    isDraft: process.env.PR_IS_DRAFT !== 'false',
    bridgeJobId: process.env.BRIDGE_JOB_ID,
    dryRun: process.env.PR_DRY_RUN !== 'false'
  }, {
    additionalMetadata: {
      source: 'pr-event-worker-trigger',
      bridge_job_id: process.env.BRIDGE_JOB_ID || ''
    }
  });

  console.log(JSON.stringify({
    eventKey: HATCHET_EVENTS.GITHUB_PR_CREATED,
    eventId: event.eventId
  }, null, 2));
}

if (require.main === module) {
  main().catch((error) => {
    console.error(error);
    process.exit(1);
  });
}
