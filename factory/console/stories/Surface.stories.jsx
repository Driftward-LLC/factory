import { Card, Heading, EmptyState, Spinner } from '@driftward/ui';

export default {
  title: 'Components/Surface', component: Card, tags: ['autodocs'],
  parameters: { docs: { description: { component: 'Direct Astryx Card, Heading, EmptyState and Spinner exports compose Factory sections without a second implementation. Card uses static padding steps and no elevation. Heading retains the semantic level. EmptyState names missing evidence; loading never implies an empty queue. Avoid runtime dimensions and inline styles under Factory CSP.' } } },
};
export const EvidenceSurface = { render: () => <Card padding={5}><Heading level={2}>Verification evidence</Heading><p className="muted">Newest returned attempt. Stored evidence is not live approval.</p></Card> };
export const Unavailable = { render: () => <Card padding={4}><EmptyState title="Work unavailable" description="No successful snapshot has been retrieved. Refresh to reread records." isCompact /></Card> };
export const Loading = { render: () => <Card padding={4}><Spinner label="Retrieving evidence…" size="sm" /></Card> };
