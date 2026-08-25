import { hatchet } from './hatchet-client';
import { prEventObserver } from './workflows/pr-event-observer';

async function main() {
  const worker = await hatchet.worker('pr-event-worker', {
    workflows: [prEventObserver]
  });

  await worker.start();
}

if (require.main === module) {
  main().catch((error) => {
    console.error(error);
    process.exit(1);
  });
}
