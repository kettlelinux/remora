// Plugin compatibility answers (docs/PROTOCOL.md, "Plugin compatibility"): the badge next to a
// store or installed plugin, and the sentence saying what it needs.
import { t, type StringKey } from '../i18n';

export type CompatState = 'works' | 'partial' | 'untested' | 'unsupported' | 'broken';

export interface Compat {
  state: CompatState;
  /** Device facts it needs and this device lacks (x86_64, steamdeck, python:<module>, ...). */
  reasons: string[];
  note: string | null;
  /** The newest plugin version the list entry was checked against. */
  checked: string | null;
  tested_here?: boolean;
  /** Installed plugins: used native files built for another CPU. */
  binaries?: string[];
  detail?: string;
}

export interface DeviceInfo {
  arch: string;
  model: string | null;
  device_id: string | null;
  cpu_vendor: string | null;
  gpu_drivers: string[];
  facts: Record<string, boolean>;
}

/** Hidden from the store unless remora.storeShowIncompatible is on; installs need developer mode. */
export const isBlocked = (c?: Compat | null): boolean => c?.state === 'unsupported' || c?.state === 'broken';

const COLORS: Record<CompatState, string> = {
  works: '#59bf40',
  partial: '#e5a50a',
  untested: '#5c6370',
  unsupported: '#d94126',
  broken: '#d94126',
};

export function factText(fact: string): string {
  if (fact.startsWith('python:')) return t('compat.need.python', { module: fact.slice('python:'.length) });
  const key = `compat.need.${fact}` as StringKey;
  const text = t(key);
  return text === key ? fact : text;
}

export function CompatBadge({ compat }: { compat?: Compat | null }) {
  if (!compat) return null;
  return (
    <span
      style={{
        background: COLORS[compat.state] ?? COLORS.untested,
        color: '#fff',
        borderRadius: 4,
        padding: '1px 6px',
        fontSize: 11,
        fontWeight: 700,
        marginLeft: 6,
        whiteSpace: 'nowrap',
      }}
    >
      {t(`compat.state.${compat.state}` as StringKey)}
    </span>
  );
}

/** "Needs an x86_64 (PC) processor." and the note, or nothing to say. */
export function compatText(compat?: Compat | null): string | undefined {
  if (!compat) return undefined;
  const needs = compat.reasons.map(factText).join(', ');
  const parts: string[] = [];
  if (compat.state === 'unsupported') parts.push(compat.detail ? t('compat.builtFor', { detail: compat.detail }) : t('compat.needs', { needs }));
  else if (compat.state === 'broken') parts.push(t('compat.broken'));
  else if (compat.state === 'untested' && needs) parts.push(t('compat.mayNeed', { needs, version: compat.checked ?? '?' }));
  else if (compat.state === 'works' && compat.tested_here) parts.push(t('compat.testedHere'));
  if (compat.note) parts.push(compat.note);
  return parts.length ? parts.join(' ') : undefined;
}

/** "AYN Odin 2 Portal · Qualcomm ARM64 · Adreno graphics" */
export function deviceText(d: DeviceInfo): string {
  const vendor = d.cpu_vendor ? t(`compat.vendor.${d.cpu_vendor}` as StringKey) : '';
  const cpu = [vendor.startsWith('compat.') ? d.cpu_vendor : vendor, d.arch === 'x86_64' ? 'x86_64' : d.arch === 'aarch64' ? 'ARM64' : d.arch].filter(Boolean).join(' ');
  const gpu = ['adreno-gpu', 'mali-gpu', 'amd-gpu', 'intel-gpu'].find((f) => d.facts[f]);
  return [d.model ?? d.device_id ?? t('compat.unknownModel'), cpu, gpu ? factText(gpu).replace(/^an? /, '') : ''].filter(Boolean).join(' · ');
}
