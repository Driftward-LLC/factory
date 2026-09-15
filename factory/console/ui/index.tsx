import type { ReactNode, ComponentProps } from 'react';
import { CheckCircledIcon, CrossCircledIcon, Half2Icon, CircleIcon } from './icons.js';

/** Native button; defaults to a non-submitting action. Supply type="submit" for forms. */
export function Button({ type = 'button', children, ...props }: ComponentProps<'button'>) {
  return <button type={type} {...props}>{children}</button>;
}

/** Polite status announcement. Empty content hides the notice without unmounting it. */
export function Notice({ id, children }: { id?: string; children?: ReactNode }) {
  return (
    <div id={id} className="notice" role="status" aria-live="polite" hidden={!children}>
      {children}
    </div>
  );
}

/** Text and icon communicate status together. Callers own domain-specific evidence labels. */
export function StatusBadge({ tone = '', children }: { tone?: '' | 'info' | 'success' | 'warning' | 'danger'; children: ReactNode }) {
  const Icon = tone === 'success' ? CheckCircledIcon
    : tone === 'danger' ? CrossCircledIcon
    : tone === 'warning' ? Half2Icon : CircleIcon;
  return <span className={'pill ' + tone}><Icon aria-hidden="true" />{children}</span>;
}

/** Opens a caller-validated destination in a new tab. This component does not validate URLs. */
export function ExternalLink({ children, ...props }: ComponentProps<'a'>) {
  return <a {...props} target="_blank" rel="noopener noreferrer">{children}</a>;
}

/** A named count with an evidence qualifier. Supply an explicit unavailable value for missing data. */
export function Metric({ name, id, value, children }: { name: string; id?: string; value: ReactNode; children?: ReactNode }) {
  return (
    <div className="metric">
      <span>{name}</span>
      <strong id={id}>{value}</strong>
      <small>{children}</small>
    </div>
  );
}

/** Labeled inspection metadata, with an explicit fallback for absent content. */
export function SummaryItem({ title, children }: { title: string; children?: ReactNode }) {
  return <div className="summary-item"><span>{title}</span><strong>{children ?? 'Unavailable'}</strong></div>;
}

/** Decorative Factory mark. Always pair with a visible product name. */
export function BrandMark() {
  return <span className="brand-mark" aria-hidden="true">F</span>;
}
