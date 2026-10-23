import { BridgeClient } from '@driftward/hatchet-worker-shared';
import { ConcurrencyLimitStrategy } from '@hatchet-dev/typescript-sdk/v1';
import { hatchet } from '../hatchet-client';
import { IngressObservationClient } from '../ingress-observation-client';
import { inspectBridgeLedger } from './bridge-ledger-inspection';
import { reconciliationInput, type BridgeStateReconciliationInput } from './reconciliation-input';

export type BridgeStateReconciliationOutput = {
  'inspect-bridge-ledger': Awaited<ReturnType<typeof inspectBridgeLedger>>;
};

export const bridgeStateReconciliation = hatchet.workflow<BridgeStateReconciliationInput, BridgeStateReconciliationOutput>({
  name: 'reconciliation-bridge-state-check',
  on: { cron: '*/15 * * * *' },
  concurrency: {
    expression: '"reconciliation-bridge-state-check"',
    maxRuns: 1,
    limitStrategy: ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN,
  },
});

bridgeStateReconciliation.task({
  name: 'inspect-bridge-ledger',
  executionTimeout: '60s',
  fn: async (input, ctx) => inspectBridgeLedger(reconciliationInput(input), {
    bridge: new BridgeClient(),
    observations: new IngressObservationClient(),
    runs: hatchet.runs,
    pushEvent: (event, data, options) => hatchet.events.push(event, data, options),
    log: (message, details) => ctx.logger.info(message, details),
  }),
});
