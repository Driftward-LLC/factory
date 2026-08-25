import { hatchet } from './hatchet-client';
import { bridgeExecutorCreateDraftPr } from './workflows/create-draft-pr-executor';

async function main() {
  const worker = await hatchet.worker('bridge-executor-worker', {
    workflows: [bridgeExecutorCreateDraftPr]
  });

  await worker.start();
}

if (require.main === module) {
  main().catch((error) => {
    console.error(error);
    process.exit(1);
  });
}
