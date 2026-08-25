import { bridgeStateReconciliation } from '../workflows/bridge-state-reconciliation';

async function main() {
  const result = await bridgeStateReconciliation.run({
    requestId: `manual-${Date.now()}`,
    maxJobs: Number.parseInt(process.env.RECONCILIATION_MAX_JOBS || '50', 10),
    maxEventsPerJob: Number.parseInt(process.env.RECONCILIATION_MAX_EVENTS_PER_JOB || '100', 10),
    staleAfterMinutes: Number.parseInt(process.env.RECONCILIATION_STALE_MINUTES || '30', 10),
    emitWhenClean: process.env.RECONCILIATION_EMIT_WHEN_CLEAN === '1'
  });

  console.log(JSON.stringify(result['inspect-bridge-ledger'], null, 2));
  process.exit(0);
}

if (require.main === module) {
  main().catch((error) => {
    console.error(error);
    process.exit(1);
  });
}
