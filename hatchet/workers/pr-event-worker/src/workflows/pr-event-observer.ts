import {
  BRIDGE_LOOKUP_STATUSES,
  BridgeClient,
  DECISIONS,
  defaultReasonCodeForHatchetEvent,
  HATCHET_EVENTS,
  type BridgeJob,
  type BridgeLookupStatus
} from '@driftward/hatchet-worker-shared';
import { hatchet } from '../hatchet-client';

export type PrEventInput = {
  repo?: string;
  prNumber?: number;
  prUrl?: string;
  title?: string;
  state?: string;
  merged?: boolean;
  createdAt?: string;
  closedAt?: string;
  updatedAt?: string;
  headRefName?: string;
  headSha?: string;
  baseRefName?: string;
  linearIssues?: string[];
  dispatchEligible?: boolean;
  decision?: string;
  reasonCode?: string;
  isDraft?: boolean;
  bridgeJobId?: string;
  dryRun?: boolean;
  checkConclusion?: string;
  checkStatus?: string;
  checkSuiteId?: string;
  checkRunCount?: number;
  pullRequestNumbers?: number[];
  reviewState?: string;
  reviewId?: string;
  reviewCommitSha?: string;
  reviewSubmittedAt?: string;
  actorKind?: string;
};

export type PrEventOutput = {
  'normalize-pr-event': {
    ok: boolean;
    eventKind: string;
    repo: string;
    prNumber: number;
    prUrl: string;
    title: string;
    state: string;
    merged: boolean;
    createdAt?: string;
    closedAt?: string;
    updatedAt?: string;
    headRefName?: string;
    headSha?: string;
    baseRefName?: string;
    linearIssues: string[];
    dispatchEligible: boolean;
    decision: string;
    reasonCode: string;
    isDraft: boolean;
    bridgeJobId?: string;
    bridgeJobState?: string;
    bridgeIdempotencyKey?: string;
    bridgeLookupStatus: BridgeLookupStatus;
    bridgeLookupError?: string;
    dryRun: boolean;
    requiresFreshVerification: boolean;
    checkConclusion?: string;
    checkStatus?: string;
    checkSuiteId?: string;
    checkRunCount?: number;
    pullRequestNumbers: number[];
    reviewState?: string;
    reviewId?: string;
    reviewCommitSha?: string;
    reviewSubmittedAt?: string;
    actorKind?: string;
    summary: string;
  };
};

export const prEventObserver = hatchet.workflow<PrEventInput, PrEventOutput>({
  name: 'pr-event-observer',
  onEvents: [
    HATCHET_EVENTS.GITHUB_PR_CREATED,
    HATCHET_EVENTS.GITHUB_PR_SYNCHRONIZED,
    HATCHET_EVENTS.GITHUB_PR_REOPENED,
    HATCHET_EVENTS.GITHUB_PR_CLOSED_UNMERGED,
    HATCHET_EVENTS.GITHUB_PR_CLOSED_MERGED,
    HATCHET_EVENTS.GITHUB_CHECKS_COMPLETED,
    HATCHET_EVENTS.GITHUB_REVIEW_SUBMITTED
  ]
});

prEventObserver.task({
  name: 'normalize-pr-event',
  executionTimeout: '30s',
  fn: async (input, ctx) => {
    const eventKey = ctx.triggeringEventKey() || 'unknown';
    const requiresFreshVerification =
      eventKey === HATCHET_EVENTS.GITHUB_PR_SYNCHRONIZED ||
      eventKey === HATCHET_EVENTS.GITHUB_PR_REOPENED;
    const repo = input.repo || '';
    const prNumber = Number(input.prNumber || 0);
    const prUrl = input.prUrl || '';
    const title = input.title || '';
    const state = input.state || 'unknown';
    const merged = Boolean(input.merged);
    const createdAt = input.createdAt;
    const closedAt = input.closedAt;
    const updatedAt = input.updatedAt;
    const headRefName = input.headRefName;
    const headSha = input.headSha;
    const baseRefName = input.baseRefName;
    const linearIssues = Array.isArray(input.linearIssues)
      ? input.linearIssues.filter((issue): issue is string => typeof issue === 'string' && issue.length > 0)
      : [];
    const dispatchEligible = input.dispatchEligible === undefined ? false : Boolean(input.dispatchEligible);
    const decision = input.decision || DECISIONS.OBSERVE_ONLY;
    const reasonCode = input.reasonCode || defaultReasonCodeForHatchetEvent(eventKey);
    const isDraft = Boolean(input.isDraft);
    const dryRun = Boolean(input.dryRun);
    const pullRequestNumbers = Array.isArray(input.pullRequestNumbers)
      ? input.pullRequestNumbers.filter((number): number is number => Number.isInteger(number) && number > 0)
      : [];
    const bridgeLookup = dryRun
      ? { status: BRIDGE_LOOKUP_STATUSES.SKIPPED }
      : await lookupBridgeJob({
        bridgeJobId: input.bridgeJobId,
        prUrl
      });

    ctx.logger.info('Normalized PR event', {
      eventKey,
      repo,
      prNumber,
      dispatchEligible,
      decision,
      dryRun,
      bridgeLookupStatus: bridgeLookup.status
    });

    const bridgeJob = 'job' in bridgeLookup ? bridgeLookup.job : undefined;

    return {
      ok: true,
      eventKind: eventKey,
      repo,
      prNumber,
      prUrl,
      title,
      state,
      merged,
      createdAt,
      closedAt,
      updatedAt,
      headRefName,
      headSha,
      baseRefName,
      linearIssues,
      dispatchEligible,
      decision,
      reasonCode,
      isDraft,
      bridgeJobId: input.bridgeJobId || bridgeJob?.job_id,
      bridgeJobState: bridgeJob?.state,
      bridgeIdempotencyKey: bridgeJob?.idempotency_key,
      bridgeLookupStatus: bridgeLookup.status,
      bridgeLookupError: 'error' in bridgeLookup ? bridgeLookup.error : undefined,
      dryRun,
      requiresFreshVerification,
      checkConclusion: input.checkConclusion,
      checkStatus: input.checkStatus,
      checkSuiteId: input.checkSuiteId,
      checkRunCount: input.checkRunCount,
      pullRequestNumbers,
      reviewState: input.reviewState,
      reviewId: input.reviewId,
      reviewCommitSha: input.reviewCommitSha,
      reviewSubmittedAt: input.reviewSubmittedAt,
      actorKind: input.actorKind,
      summary: `Observed ${eventKey} for ${repo || 'unknown repo'}#${prNumber || 0}; no Hermes dispatch`
    };
  }
});

async function lookupBridgeJob(input: {
  bridgeJobId?: string;
  prUrl?: string;
}): Promise<
  | { status: typeof BRIDGE_LOOKUP_STATUSES.MATCHED; job: BridgeJob }
  | { status: typeof BRIDGE_LOOKUP_STATUSES.NOT_FOUND }
  | { status: typeof BRIDGE_LOOKUP_STATUSES.ERROR; error: string }
> {
  if (!input.bridgeJobId && !input.prUrl) {
    return { status: BRIDGE_LOOKUP_STATUSES.NOT_FOUND };
  }

  const bridge = new BridgeClient();
  try {
    if (input.bridgeJobId) {
      return {
        status: BRIDGE_LOOKUP_STATUSES.MATCHED,
        job: await bridge.getJob(input.bridgeJobId)
      };
    }

    const lookupLimit = Number(process.env.PR_EVENT_BRIDGE_LOOKUP_LIMIT || '100');
    const { jobs } = await bridge.listJobs(Number.isFinite(lookupLimit) ? lookupLimit : 100);
    const job = jobs.find((candidate) => candidate.pr_url === input.prUrl);
    return job
      ? { status: BRIDGE_LOOKUP_STATUSES.MATCHED, job }
      : { status: BRIDGE_LOOKUP_STATUSES.NOT_FOUND };
  } catch (error) {
    return {
      status: BRIDGE_LOOKUP_STATUSES.ERROR,
      error: error instanceof Error ? error.message : String(error)
    };
  }
}
