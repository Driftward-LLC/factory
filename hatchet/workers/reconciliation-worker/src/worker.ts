import { loadFactoryOrganization } from '@factory/hatchet-worker-shared';
import { hatchet } from './hatchet-client';
import { bridgeStateReconciliation } from './workflows/bridge-state-reconciliation';

async function main() {
  loadFactoryOrganization();
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
