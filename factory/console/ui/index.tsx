import type { ReactNode, ComponentProps } from 'react';
import { Button as AstryxButton, type ButtonVariant } from '@astryxdesign/core/Button';
import { Badge } from '@astryxdesign/core/Badge';
import { Banner } from '@astryxdesign/core/Banner';
import { Link } from '@astryxdesign/core/Link';
import { TextInput as AstryxTextInput, type TextInputProps } from '@astryxdesign/core/TextInput';
import { Theme } from '@astryxdesign/core/theme';
import { neutralTheme } from '@astryxdesign/theme-neutral/built';
import { CheckCircledIcon, CrossCircledIcon, Half2Icon, CircleIcon } from './icons.js';

export { Card } from '@astryxdesign/core/Card';
export { Heading } from '@astryxdesign/core/Heading';
export { MetadataList, MetadataListItem } from '@astryxdesign/core/MetadataList';
export { Link } from '@astryxdesign/core/Link';
export { EmptyState } from '@astryxdesign/core/EmptyState';
export { Spinner } from '@astryxdesign/core/Spinner';

/** Built dark theme: all styling ships in styles.css, without runtime style injection. */
export function FactoryTheme({ children }: { children: ReactNode }) {
  return <Theme theme={neutralTheme} mode="dark">{children}</Theme>;
}

export interface FactoryTextInputProps extends TextInputProps {
  /** Native browser required validation, checked when the form submits. */
  required?: boolean;
  /** Native maximum number of input characters. */
  maxLength?: number;
  /** Native browser pattern validation, checked when the form submits. */
  pattern?: string;
  /** Native browser spelling hint. */
  spellCheck?: ComponentProps<'input'>['spellCheck'];
}

/**
 * Astryx controlled field with native lookup and ownership form validation.
 * label names the input; value is the controlled string. onChange(value, event)
 * receives the new string and change event. isDisabled blocks editing and focus.
 * isRequired supplies Astryx's required label/ARIA; native required enforces
 * browser form validation. maxLength limits characters; pattern validates the
 * value at form submission; spellCheck controls the browser spelling hint.
 * Style the field with containerClassName and its control with controlClassName.
 */
export function TextInput(props: FactoryTextInputProps) {
  return <AstryxTextInput {...props} />;
}

type ButtonName = { 'aria-label': string } | { 'aria-labelledby': string };
export type ButtonProps = Omit<ComponentProps<'button'>, 'children'> & { variant?: ButtonVariant } & (
  { children?: ReactNode; loading?: false }
  | { children: string | number; loading?: boolean }
  | (ButtonName & { children?: ReactNode; loading?: boolean })
);

/**
 * Native action contract over Astryx. Loading blocks interaction and keeps a name.
 * Plain text/number labels name themselves. Composed loading labels require an
 * explicit aria-label or aria-labelledby; idle compositions keep native naming.
 */
export function Button({ type = 'button', disabled, loading = false, variant = 'secondary', children, ...props }:
  ButtonProps) {
  const textLabel = typeof children === 'string' || typeof children === 'number' ? String(children) : '';
  if (loading && !textLabel.trim() && !props['aria-label']?.trim() && !props['aria-labelledby']?.trim()) {
    throw new Error('Loading buttons with composed children require aria-label or aria-labelledby.');
  }
  return <AstryxButton type={type} isDisabled={disabled} isLoading={loading} variant={variant}
    label={props['aria-label']?.trim() ? props['aria-label'] : textLabel} {...props}>
    <span className="button-content">{children}</span>
  </AstryxButton>;
}

/** Polite status announcement. Empty content hides the notice without unmounting it. */
export function Notice({ id, children }: { id?: string; children?: ReactNode }) {
  return (
    <div id={id} className="notice" role="status" aria-live="polite" hidden={!children}>
      {children && <Banner status="warning" title={<span className="notice-message">{children}</span>} collapsible={false} />}
    </div>
  );
}

/** Text and icon communicate status together. Callers own domain-specific evidence labels. */
export function StatusBadge({ tone = '', children }: { tone?: '' | 'info' | 'success' | 'warning' | 'danger'; children: ReactNode }) {
  const Icon = tone === 'success' ? CheckCircledIcon
    : tone === 'danger' ? CrossCircledIcon
    : tone === 'warning' ? Half2Icon : CircleIcon;
  const variant = tone === 'danger' ? 'red' : tone === 'warning' ? 'yellow'
    : tone === 'success' ? 'green' : tone === 'info' ? 'blue' : 'neutral';
  return <Badge className={'pill ' + tone} variant={variant} label={children} icon={<Icon aria-hidden="true" />} />;
}

/** Opens a caller-validated destination in a new tab. This component does not validate URLs. */
export function ExternalLink({ children, ...props }: ComponentProps<'a'>) {
  return <Link {...props} type="inherit" color="inherit" target="_blank" rel="noopener noreferrer">{children}</Link>;
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
