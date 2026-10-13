import { FactoryTheme, Card, Heading, Button, StatusBadge, SummaryItem } from '@driftward/ui';
import { ReloadIcon } from '@driftward/ui/icons';

export default {
  title: 'Foundations/FactoryTheme', component: FactoryTheme, tags: ['autodocs'],
  parameters: { docs: { description: { component: 'Factory uses the precompiled Astryx neutral theme in dark mode with local platform fonts and static operator tokens. Import @driftward/ui/styles.css once and wrap the application in FactoryTheme. Built theme CSS is bundled locally; it does not inject styles or require a relaxed CSP. These records are fictional.' } } },
};
export const OperatorWorkbench = {
  render: () => <FactoryTheme><Card padding={5}>
    <Heading level={2}>Recorded execution</Heading>
    <SummaryItem title="Latest stage"><StatusBadge tone="warning">Pending verification</StatusBadge></SummaryItem>
    <p className="muted">A recorded artifact is awaiting evidence. This is not current PR approval.</p>
    <Button variant="ghost"><ReloadIcon aria-hidden="true" />Refresh evidence</Button>
  </Card></FactoryTheme>,
};
