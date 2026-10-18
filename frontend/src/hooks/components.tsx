// Lookup 13: the @decky/ui components Remora's own pages use. @decky/ui finds each one in
// Steam's webpack by heuristics; one that is missing would throw when rendered. Remora renders
// through these wrappers, which fall back to plain elements, so a missing component makes a
// page plainer instead of breaking it.
import * as DFL from '@decky/ui';
import type { ComponentType, CSSProperties, ReactNode } from 'react';
import { SteamAny } from '../steam/globals';
import { HookHandle } from './status';

const hook = new HookHandle('dfl-components', "parts of Remora's pages render as plain elements");

type AnyProps = Record<string, SteamAny> & { children?: ReactNode };

const box: CSSProperties = { padding: '8px 0' };

function FallbackSection({ title, children }: AnyProps) {
  return (
    <div style={{ padding: '0 16px', marginBottom: 12 }}>
      {title && <div style={{ fontWeight: 700, textTransform: 'uppercase', fontSize: 12, opacity: 0.7, margin: '8px 0' }}>{title}</div>}
      {children}
    </div>
  );
}

function FallbackRow({ children }: AnyProps) {
  return <div style={box}>{children}</div>;
}

function FallbackButton({ children, onClick, disabled, label, description, style, autoFocus }: AnyProps) {
  return (
    <div style={box}>
      {label && <div>{label}</div>}
      <button disabled={disabled} onClick={onClick} autoFocus={Boolean(autoFocus)} style={{ width: '100%', padding: 8, ...style }}>
        {children}
      </button>
      {description && <div style={{ fontSize: 12, opacity: 0.7 }}>{description}</div>}
    </div>
  );
}

function FallbackToggle({ label, description, checked, onChange, disabled }: AnyProps) {
  return (
    <label style={{ ...box, display: 'flex', justifyContent: 'space-between', gap: 8 }}>
      <span>
        {label}
        {description && <div style={{ fontSize: 12, opacity: 0.7 }}>{description}</div>}
      </span>
      <input type="checkbox" checked={Boolean(checked)} disabled={disabled} onChange={(e) => onChange?.(e.currentTarget.checked)} />
    </label>
  );
}

function FallbackField({ label, description, children }: AnyProps) {
  return (
    <div style={{ ...box, display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'center' }}>
      <div>
        {label}
        {description && <div style={{ fontSize: 12, opacity: 0.7 }}>{description}</div>}
      </div>
      <div>{children}</div>
    </div>
  );
}

function FallbackFocusable({ children, style, className, onActivate, onClick, ...rest }: AnyProps) {
  const pass: AnyProps = {};
  for (const k of ['flow-children', 'onKeyDown']) if (rest[k] !== undefined) pass[k] = rest[k];
  return (
    <div style={style} className={className} onClick={onClick ?? onActivate} {...pass}>
      {children}
    </div>
  );
}

function FallbackTextField({ label, value, onChange, placeholder, description }: AnyProps) {
  return (
    <div style={box}>
      {label && <div>{label}</div>}
      <input style={{ width: '100%' }} value={value ?? ''} placeholder={placeholder} onChange={onChange} />
      {description && <div style={{ fontSize: 12, opacity: 0.7 }}>{description}</div>}
    </div>
  );
}

function FallbackDropdown({ rgOptions, selectedOption, onChange, disabled }: AnyProps) {
  const opts: { data: unknown; label: ReactNode }[] = rgOptions ?? [];
  const idx = opts.findIndex((o) => o.data === selectedOption);
  return (
    <select disabled={disabled} value={idx} onChange={(e) => onChange?.(opts[Number(e.currentTarget.value)])}>
      {opts.map((o, i) => (
        <option key={i} value={i}>
          {typeof o.label === 'string' ? o.label : String(o.data)}
        </option>
      ))}
    </select>
  );
}

function FallbackSpinner(props: AnyProps) {
  return <div style={{ opacity: 0.7, ...props.style }}>…</div>;
}

function FallbackProgress({ nProgress, sOperationText, indeterminate }: AnyProps) {
  return (
    <div style={box}>
      {sOperationText && <div>{sOperationText}</div>}
      <progress style={{ width: '100%' }} max={100} value={indeterminate ? undefined : nProgress} />
    </div>
  );
}

function FallbackSidebar({ title, pages }: AnyProps) {
  const visible = (pages as SteamAny[]).filter((p) => p !== 'separator' && p.visible !== false);
  return (
    <div style={{ padding: '48px 24px' }}>
      {title && <h2>{title}</h2>}
      {visible.map((p, i) => (
        <section key={i}>
          <h3>{p.title}</h3>
          {p.content}
        </section>
      ))}
    </div>
  );
}

function FallbackModal({ children, closeModal, onCancel, strTitle, strDescription, onOK, strOKButtonText, strCancelButtonText }: AnyProps) {
  const close = closeModal ?? onCancel;
  return (
    <div style={{ padding: 24, background: '#23262e' }}>
      {strTitle && <h2>{strTitle}</h2>}
      {strDescription && <div>{strDescription}</div>}
      {children}
      <div style={{ display: 'flex', gap: 8, marginTop: 16 }}>
        {onOK && <button onClick={() => (onOK(), close?.())}>{strOKButtonText ?? 'OK'}</button>}
        {close && <button onClick={() => (onCancel ? onCancel() : close())}>{strCancelButtonText ?? 'Cancel'}</button>}
      </div>
    </div>
  );
}

const missing: string[] = [];

function pick<P>(name: keyof typeof DFL, fallback: ComponentType<P>): ComponentType<P> {
  const c = (DFL as unknown as Record<string, unknown>)[name as string];
  if (c && (typeof c === 'function' || typeof c === 'object')) return c as ComponentType<P>;
  missing.push(String(name));
  return fallback;
}

export const PanelSection = pick<DFL.PanelSectionProps>('PanelSection', FallbackSection);
export const PanelSectionRow = pick<DFL.PanelSectionRowProps>('PanelSectionRow', FallbackRow);
export const ButtonItem = pick<DFL.ButtonItemProps>('ButtonItem', FallbackButton);
export const DialogButton = pick<DFL.DialogButtonProps>('DialogButton', FallbackButton);
export const ToggleField = pick<DFL.ToggleFieldProps>('ToggleField', FallbackToggle);
export const Field = pick<DFL.FieldProps>('Field', FallbackField);
export const Focusable = pick<DFL.FocusableProps>('Focusable', FallbackFocusable);
export const TextField = pick<DFL.TextFieldProps>('TextField', FallbackTextField);
export const Dropdown = pick<DFL.DropdownProps>('Dropdown', FallbackDropdown);
export const Spinner = pick<AnyProps>('Spinner', FallbackSpinner);
export const SteamSpinner = pick<AnyProps>('SteamSpinner', FallbackSpinner);
export const ProgressBarWithInfo = pick<DFL.ProgressBarWithInfoProps>('ProgressBarWithInfo', FallbackProgress);
export const SidebarNavigation = pick<DFL.SidebarNavigationProps>('SidebarNavigation', FallbackSidebar);
export const ModalRoot = pick<DFL.ModalRootProps>('ModalRoot', FallbackModal);
export const ConfirmModal = pick<DFL.ConfirmModalProps>('ConfirmModal', FallbackModal);
export const ScrollPanelGroup = pick<AnyProps>('ScrollPanelGroup', FallbackRow);

export function checkComponents(): void {
  if (!missing.length) hook.ok('@decky/ui exports');
  else hook.degraded('plain-element fallbacks', `missing: ${missing.join(', ')}`);
}
