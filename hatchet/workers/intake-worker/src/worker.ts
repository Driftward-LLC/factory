import { loadFactoryOrganization } from '@factory/hatchet-worker-shared';
import { hatchet } from './hatchet-client';
import { linearReadyIntake } from './workflows/linear-ready-intake';

async function main() {
  loadFactoryOrganization();
  const worker = await hatchet.worker('intake-worker', {
    workflows: [linearReadyIntake]
  });

  await worker.start();
}

if (require.main === module) {
  main().catch((error) => {
    console.error(error);
    process.exit(1);
  });
}
