// QAM list order: pluginOrder (by index; unknown names after, in load order), or by name when
// sortPlugins is on. Hidden plugins and plugins without `content` are left out.
import type { LoaderSettings } from './settings';
import type { LoadedPlugin } from './plugin';

export function orderPlugins<T extends { name: string }>(plugins: T[], settings: Pick<LoaderSettings, 'pluginOrder' | 'sortPlugins'>): T[] {
  if (settings.sortPlugins) return [...plugins].sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }));
  const idx = new Map(settings.pluginOrder.map((n, i) => [n, i]));
  return plugins
    .map((p, i) => ({ p, i }))
    .sort((a, b) => (idx.get(a.p.name) ?? 1e6 + a.i) - (idx.get(b.p.name) ?? 1e6 + b.i))
    .map((x) => x.p);
}

export function qamPlugins(plugins: LoadedPlugin[], settings: LoaderSettings): { shown: LoadedPlugin[]; hidden: number } {
  const hidden = new Set(settings.hiddenPlugins);
  const withContent = plugins.filter((p) => p.content !== undefined && p.content !== null);
  const shown = orderPlugins(
    withContent.filter((p) => !hidden.has(p.name)),
    settings,
  );
  return { shown, hidden: withContent.length - shown.length };
}
