/** A bridge transition is durable; a transient Hatchet event push must not strand it. */
export async function emitBestEffort(effect: () => Promise<void>): Promise<void> {
  try {
    await effect();
  } catch {
    // Reconciliation/redelivery sees the durable bridge state and retries the workflow.
  }
}
