// Translation lookup. Only English ships today; other locales register here with the same keys.
import { en, type StringKey } from './en';

type Table = Partial<Record<StringKey, string>>;
const locales: Record<string, Table> = { en };
let active: Table = en;

export function setLanguage(lang: string | undefined): void {
  const l = (lang ?? '').toLowerCase();
  active = locales[l] ?? locales[l.split(/[-_]/)[0] ?? ''] ?? en;
}

export function t(key: StringKey, vars?: Record<string, string | number>): string {
  let s: string = active[key] ?? en[key] ?? key;
  if (vars) for (const [k, v] of Object.entries(vars)) s = s.split(`{${k}}`).join(String(v));
  return s;
}

export type { StringKey };
