export type ProviderSource = 'github' | 'linear';

export type ProviderObservation = {
  eventKey: string;
  source: ProviderSource;
  subject: string;
  observedAt: string;
  providerUpdatedAt?: string;
  deliveryId?: string;
  github?: { repo?: string; prUrl?: string; headRefName?: string; headSha?: string; isDraft?: boolean; state?: string; merged?: boolean; linearIssues?: string[] };
  linear?: { projectId?: string; status?: string; blockedByCount?: number; dispatchEligible?: boolean };
  hatchetPublished?: boolean;
  decision?: string;
  reasonCode?: string;
};

export type ObservationLookup =
  | { status: 'matched'; observation: ProviderObservation }
  | { status: 'not_found' }
  | { status: 'timeout' | 'unavailable' | 'malformed' | 'auth' | 'stale' };

export type ObservationClient = { latest(source: ProviderSource, subject: string): Promise<ObservationLookup> };
export type ObservationInventoryLookup =
  | { status: 'matched'; observations: ProviderObservation[]; complete: boolean }
  | { status: 'timeout' | 'unavailable' | 'malformed' | 'auth' };

class SerializedRequestGate {
  private active = false;
  private readonly waiters: Array<() => void> = [];

  async run<T>(operation: () => Promise<T>): Promise<T> {
    await this.acquire();
    try {
      return await operation();
    } finally {
      this.release();
    }
  }

  private acquire(): Promise<void> {
    if (!this.active) {
      this.active = true;
      return Promise.resolve();
    }
    return new Promise((resolve) => this.waiters.push(resolve));
  }

  private release(): void {
    const next = this.waiters.shift();
    if (next) next();
    else this.active = false;
  }
}

// The ingress service intentionally uses Python HTTPServer, which serves one
// request at a time. This module is shared by every workflow invocation in a
// worker process, so separate reconciliation runs cannot flood that listener.
const ingressRequestGate = new SerializedRequestGate();

export class IngressObservationClient implements ObservationClient {
  private readonly baseUrl: URL;
  private readonly timeoutMs: number;
  private readonly maxAgeMs: number;

  constructor(options: { baseUrl?: string; timeoutMs?: number; allowDockerServiceHost?: boolean } = {}) {
    const allowDockerServiceHost = options.allowDockerServiceHost
      ?? process.env.HATCHET_RECONCILIATION_ALLOW_DOCKER_INGRESS === '1';
    this.baseUrl = ingressUrl(
      options.baseUrl || process.env.HATCHET_EVENT_INGRESS_URL || 'http://127.0.0.1:19878',
      allowDockerServiceHost,
    );
    this.timeoutMs = boundedInteger(options.timeoutMs || Number(process.env.RECONCILIATION_OBSERVATION_TIMEOUT_MS), 250, 3000, 1500);
    this.maxAgeMs = Math.min(7 * 24 * 60 * 60 * 1000, Math.max(60 * 1000, Number(process.env.RECONCILIATION_OBSERVATION_MAX_AGE_MS) || 24 * 60 * 60 * 1000));
  }

  async latest(source: ProviderSource, subject: string): Promise<ObservationLookup> {
    return ingressRequestGate.run(() => this.requestLatest(source, subject));
  }

  async inventory(limit: number): Promise<ObservationInventoryLookup> {
    return ingressRequestGate.run(() => this.requestInventory(limit));
  }

  private async requestLatest(source: ProviderSource, subject: string): Promise<ObservationLookup> {
    const url = new URL('/observations/latest', this.baseUrl);
    url.searchParams.set('source', source);
    url.searchParams.set('subject', subject);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const response = await fetch(url, { signal: controller.signal });
      if (response.status === 401 || response.status === 403) return { status: 'auth' };
      if (!response.ok) return { status: 'unavailable' };
      const body = await response.json() as Record<string, unknown>;
      if (body.status === 'not_found') return { status: 'not_found' };
      if (body.status !== 'matched' || !observation(body.observation, source, subject)) return { status: 'malformed' };
      const timestamp = body.observation.providerUpdatedAt || body.observation.observedAt;
      const parsed = Date.parse(timestamp);
      if (!Number.isFinite(parsed)) return { status: 'malformed' };
      if (parsed < Date.now() - this.maxAgeMs) return { status: 'stale' };
      return { status: 'matched', observation: body.observation };
    } catch (error) {
      return { status: error instanceof Error && error.name === 'AbortError' ? 'timeout' : 'unavailable' };
    } finally {
      clearTimeout(timer);
    }
  }

  private async requestInventory(limit: number): Promise<ObservationInventoryLookup> {
    const url = new URL('/reconciliation/observations', this.baseUrl);
    url.searchParams.set('limit', String(Math.min(250, Math.max(1, Math.floor(limit)))));
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const response = await fetch(url, { signal: controller.signal });
      if (response.status === 401 || response.status === 403) return { status: 'auth' };
      if (!response.ok) return { status: 'unavailable' };
      const body = await response.json() as Record<string, unknown>;
      if (!Array.isArray(body.observations) || typeof body.total !== 'number') return { status: 'malformed' };
      const observations = body.observations.filter((item): item is ProviderObservation => {
        if (!item || typeof item !== 'object') return false;
        const candidate = item as ProviderObservation;
        return (candidate.source === 'github' || candidate.source === 'linear') && observation(candidate, candidate.source, candidate.subject) && isValidObservationTimestamp(candidate.observedAt) && (!candidate.providerUpdatedAt || isValidObservationTimestamp(candidate.providerUpdatedAt));
      });
      if (observations.length !== body.observations.length) return { status: 'malformed' };
      return { status: 'matched', observations, complete: body.total <= observations.length };
    } catch (error) {
      return { status: error instanceof Error && error.name === 'AbortError' ? 'timeout' : 'unavailable' };
    } finally {
      clearTimeout(timer);
    }
  }

}

function ingressUrl(value: string, allowDockerServiceHost: boolean): URL {
  const url = new URL(value);
  const loopback = ['127.0.0.1', 'localhost', '::1'].includes(url.hostname);
  const dockerService = allowDockerServiceHost && url.hostname === 'event-ingress';
  if (url.protocol !== 'http:' || (!loopback && !dockerService)) {
    throw new Error('HATCHET_EVENT_INGRESS_URL must use HTTP loopback or the Factory Docker ingress service hostname');
  }
  return url;
}

function boundedInteger(value: number, min: number, max: number, fallback: number): number {
  return Number.isFinite(value) ? Math.min(max, Math.max(min, Math.floor(value))) : fallback;
}

function observation(value: unknown, source: ProviderSource, subject: string): value is ProviderObservation {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const record = value as Record<string, unknown>;
  return record.source === source && record.subject === subject && typeof record.eventKey === 'string' && typeof record.observedAt === 'string' && isValidObservationTimestamp(record.observedAt) && (typeof record.providerUpdatedAt === 'undefined' || (typeof record.providerUpdatedAt === 'string' && isValidObservationTimestamp(record.providerUpdatedAt)));
}

export function isValidObservationTimestamp(value: string): boolean {
  return Number.isFinite(Date.parse(value));
}
