import { hatchet } from './hatchet-client';
import { createDraftPrDispatch } from './workflows/create-draft-pr-dispatch';

async function main() {
  const worker = await hatchet.worker('execution-dispatch-worker', {
    workflows: [createDraftPrDispatch]
  });

  await worker.start();
}

if (require.main === module) {
  main().catch((error) => {
    console.error(error);
    process.exit(1);
  });
}
