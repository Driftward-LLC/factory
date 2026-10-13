import { MetadataList, MetadataListItem, StatusBadge } from '@driftward/ui';

export default {
  title: 'Components/Metadata', component: MetadataList, tags: ['autodocs'],
  parameters: { docs: { description: { component: 'Astryx MetadataList and MetadataListItem render semantic definition lists. Use columns="multi" for responsive evidence, or "single" for a simple list. Supply explicit unavailable values. Numeric columns and custom label widths use inline styles; Factory uses static responsive CSS with the multi layout.' } } },
};
export const RecordedEvidence = {
  render: () => <MetadataList columns="multi">
    <MetadataListItem label="Recorded stage"><StatusBadge tone="success">Verification recorded</StatusBadge></MetadataListItem>
    <MetadataListItem label="Verified SHA">{'a'.repeat(40)}</MetadataListItem>
    <MetadataListItem label="Observed PR state">Open · stored observation only</MetadataListItem>
    <MetadataListItem label="Independent review">Unavailable in this view</MetadataListItem>
  </MetadataList>,
};
export const Unavailable = {
  render: () => <MetadataList columns="single">
    <MetadataListItem label="Verified SHA">Unavailable</MetadataListItem>
    <MetadataListItem label="PR observed">Unavailable</MetadataListItem>
  </MetadataList>,
};
