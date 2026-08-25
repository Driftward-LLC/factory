import { hatchet } from './hatchet-client';
import { bridgeStateReconciliation } from './workflows/bridge-state-reconciliation';

async function main() {
  const worker = await hatchet.worker('reconciliation-worker', {
    workflows: [bridgeStateReconciliation]
  });

  await worker.start();
}

if (require.main === module) {
  main().catch((error) => {
    console.error(error);
    process.exit(1);
  });
}
