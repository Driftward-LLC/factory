export type HatchetRunLookup =
  | { status: 'matched'; runStatus: string; taskExternalIds: string[] }
  | { status: 'not_found' | 'timeout' | 'unavailable' | 'malformed' | 'auth' };

export type HatchetRunSummary = { workflowRunId: string; taskExternalId: string; workflowName?: string; status?: string; insertedAt?: string };
export type HatchetRunInventoryLookup =
  | { status: 'matched'; runs: HatchetRunSummary[]; complete: boolean }
  | { status: 'timeout' | 'unavailable' | 'malformed' | 'auth' };

type RunsApi = {
  getDetails(run: string): Promise<unknown>;
  list?(options: { limit: number; onlyTasks: boolean; includePayloads: boolean; workflowNames: string[] }): Promise<unknown>;
};

const gates = new WeakMap<object, SdkDeadlineGate>();

export async function observeHatchetRun(runs: RunsApi, workflowRunId: string, timeoutMs = deadlineMs()): Promise<HatchetRunLookup> {
  try {
    const detail = await gateFor(runs).run(() => runs.getDetails(workflowRunId), timeoutMs);
    if (!detail || typeof detail !== 'object') return { status: 'malformed' };
    const value = detail as { status?: unknown; taskRuns?: unknown };
    if (typeof value.status !== 'string' || !value.taskRuns || typeof value.taskRuns !== 'object') return { status: 'malformed' };
    const taskExternalIds = Object.values(value.taskRuns as Record<string, { externalId?: unknown }>)
      .map((task) => task.externalId).filter((id): id is string => typeof id === 'string');
    return { status: 'matched', runStatus: value.status, taskExternalIds };
  } catch (error) {
    return statusForError(error, true) as HatchetRunLookup;
  }
}

export async function observeHatchetRuns(runs: RunsApi, limit: number, timeoutMs = deadlineMs()): Promise<HatchetRunInventoryLookup> {
  if (!runs.list) return { status: 'malformed' };
  try {
    const response = await gateFor(runs).run(() => runs.list!({
      limit, onlyTasks: false, includePayloads: false,
      workflowNames: ['intake-linear-ready-dispatch', 'execution-dispatch-create-draft-pr'],
    }), timeoutMs);
    const rows = Array.isArray(response) ? response : (response as { rows?: unknown; data?: unknown })?.rows || (response as { data?: unknown })?.data;
    if (!Array.isArray(rows)) return { status: 'malformed' };
    const summaries = rows.map((row): HatchetRunSummary | null => {
      if (!row || typeof row !== 'object') return null;
      const item = row as Record<string, unknown>;
      if (typeof item.workflowRunExternalId !== 'string' || typeof item.taskExternalId !== 'string') return null;
      return { workflowRunId: item.workflowRunExternalId, taskExternalId: item.taskExternalId, workflowName: typeof item.workflowName === 'string' ? item.workflowName : undefined, status: typeof item.status === 'string' ? item.status : undefined, insertedAt: typeof item.taskInsertedAt === 'string' ? item.taskInsertedAt : undefined };
    });
    if (summaries.some((row) => row === null)) return { status: 'malformed' };
    return { status: 'matched', runs: summaries as HatchetRunSummary[], complete: rows.length < limit };
  } catch (error) {
    return statusForError(error, false) as HatchetRunInventoryLookup;
  }
}

class SdkDeadlineGate {
  private tail: Promise<void> = Promise.resolve();
  private pending = 0;

  run<T>(operation: () => Promise<T>, timeoutMs: number): Promise<T> {
    if (this.pending >= 16) return Promise.reject(new Error('circuit_open'));
    this.pending += 1;
    let settled = false;
    let rejectCaller: (reason: unknown) => void = () => undefined;
    const caller = new Promise<T>((resolve, reject) => {
      rejectCaller = reject;
      const timer = setTimeout(() => {
        if (!settled) {
          settled = true;
          reject(new Error('timeout'));
        }
      }, timeoutMs);
      this.tail = this.tail.then(async () => {
        if (settled) return;
        try {
          const value = await operation();
          if (!settled) {
            settled = true;
            clearTimeout(timer);
            resolve(value);
          }
        } catch (error) {
          if (!settled) {
            settled = true;
            clearTimeout(timer);
            reject(error);
          }
        }
      }).finally(() => { this.pending -= 1; });
    });
    caller.catch(() => undefined);
    return caller;
  }
}

function gateFor(runs: RunsApi): SdkDeadlineGate {
  const cached = gates.get(runs);
  if (cached) return cached;
  const gate = new SdkDeadlineGate();
  gates.set(runs, gate);
  return gate;
}

function deadlineMs(): number {
  const configured = Number(process.env.RECONCILIATION_HATCHET_TIMEOUT_MS);
  return Math.min(5000, Math.max(100, Number.isFinite(configured) ? Math.floor(configured) : 1000));
}

function statusForError(error: unknown, includeNotFound: boolean): { status: string } {
  const status = (error as { response?: { status?: number } })?.response?.status;
  if (includeNotFound && status === 404) return { status: 'not_found' };
  if (status === 401 || status === 403) return { status: 'auth' };
  if (error instanceof Error && error.message === 'timeout') return { status: 'timeout' };
  return { status: 'unavailable' };
}
