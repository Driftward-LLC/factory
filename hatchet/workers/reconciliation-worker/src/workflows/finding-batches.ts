import type { ReconciliationFindingRequest, ReconciliationFindingResponse } from '@driftward/hatchet-worker-shared';

export type FindingWriter = { recordReconciliationFindings(findings: ReconciliationFindingRequest[]): Promise<ReconciliationFindingResponse> };
export type FindingBatchResult = ReconciliationFindingResponse & { failedBatches: number };

export async function recordFindingBatches(
  writer: FindingWriter,
  findings: ReconciliationFindingRequest[],
  batchSize = 250,
): Promise<FindingBatchResult> {
  const ordered = [...findings].sort((a, b) => a.finding_key.localeCompare(b.finding_key));
  const created: string[] = [];
  const existing: string[] = [];
  let failedBatches = 0;
  for (let index = 0; index < ordered.length; index += batchSize) {
    try {
      const result = await writer.recordReconciliationFindings(ordered.slice(index, index + batchSize));
      created.push(...result.created_finding_keys);
      existing.push(...result.existing_finding_keys);
    } catch {
      failedBatches += 1;
    }
  }
  return { created_finding_keys: created, existing_finding_keys: existing, failedBatches };
}
