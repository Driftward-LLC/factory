import { hatchet } from '../hatchet-client';

type PushEventRequest = {
  eventName?: unknown;
  payload?: unknown;
  metadata?: unknown;
};

function readStdin(): Promise<string> {
  return new Promise((resolve, reject) => {
    let data = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', (chunk) => {
      data += chunk;
    });
    process.stdin.on('end', () => resolve(data));
    process.stdin.on('error', reject);
  });
}

function requireObject(value: unknown, field: string): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${field} must be an object`);
  }
  return value as Record<string, unknown>;
}

function toStringMetadata(value: Record<string, unknown>): Record<string, string> {
  return Object.fromEntries(
    Object.entries(value).map(([key, entry]) => [key, String(entry ?? '')])
  );
}

async function main() {
  const raw = await readStdin();
  const request = JSON.parse(raw || '{}') as PushEventRequest;

  if (typeof request.eventName !== 'string' || !request.eventName.trim()) {
    throw new Error('eventName is required');
  }

  const eventName = request.eventName.trim();
  const payload = requireObject(request.payload, 'payload');
  const metadata = request.metadata === undefined
    ? {}
    : requireObject(request.metadata, 'metadata');

  const event = await hatchet.events.push(eventName, payload, {
    additionalMetadata: toStringMetadata(metadata)
  });

  console.log(JSON.stringify({
    eventName,
    eventId: event.eventId
  }));
}

if (require.main === module) {
  main().catch((error) => {
    console.error(error);
    process.exit(1);
  });
}
