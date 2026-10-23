export type BridgeStateReconciliationInput = {
  requestId?: string;
  maxJobs?: number;
  maxEventsPerJob?: number;
  staleAfterMinutes?: number;
  emitWhenClean?: boolean;
};

export function reconciliationInput(input: BridgeStateReconciliationInput | null | undefined): BridgeStateReconciliationInput {
  return input ?? {};
}
