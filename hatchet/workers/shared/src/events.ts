import {
  BRIDGE_LOOKUP_STATUSES,
  DECISIONS,
  EVENT_IDS,
  EVENTS,
  HATCHET_CONTRACT,
  HATCHET_EVENT_DEFAULT_REASON_CODES_BY_WIRE_NAME,
  HATCHET_EVENT_IDS_BY_WIRE_NAME,
  HATCHET_EVENT_WIRE_NAMES,
  PROVIDER_ACTION_MAPPINGS,
  REASON_CODES
} from './contracts';

export {
  BRIDGE_LOOKUP_STATUSES,
  DECISIONS,
  EVENT_IDS,
  EVENTS,
  HATCHET_CONTRACT,
  HATCHET_EVENT_IDS_BY_WIRE_NAME,
  PROVIDER_ACTION_MAPPINGS,
  REASON_CODES
} from './contracts';
export type { BridgeLookupStatus, DecisionId, EventId, HatchetEventWireName, ReasonCodeId } from './contracts';

export const HATCHET_EVENTS = HATCHET_EVENT_WIRE_NAMES;

export type HatchetEventName = (typeof HATCHET_EVENTS)[keyof typeof HATCHET_EVENTS];

export function defaultReasonCodeForHatchetEvent(eventName: string): string {
  return (
    HATCHET_EVENT_DEFAULT_REASON_CODES_BY_WIRE_NAME[
      eventName as keyof typeof HATCHET_EVENT_DEFAULT_REASON_CODES_BY_WIRE_NAME
    ] || REASON_CODES.PR_EVENT_OBSERVED
  );
}
