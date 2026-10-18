// QAM list order: pluginOrder (by index), or by name when sortPlugins is on. Plugins not in
// pluginOrder come after, in the backend's installed order (loader/get_plugins), so the list does
// not shuffle with import completion order; names the backend does not list come last, in load
// order. Hidden plugins and plugins without `content` are left out.
import type { LoaderSettings } from './settings';
import type { LoadedPlugin } from './plugin';

export function orderPlugins<T extends { name: string }>(
  plugins: T[],
  settings: Pick<LoaderSettings, 'pluginOrder' | 'sortPlugins'>,
  installedOrder: readonly { name: string }[] = [],
): T[] {
  if (settings.sortPlugins) return [...plugins].sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }));
  const idx = new Map(settings.pluginOrder.map((n, i) => [n, i]));
  const inst = new Map(installedOrder.map((p, i) => [p.name, i]));
  const key = (name: string, i: number) => idx.get(name) ?? 1e6 + (inst.get(name) ?? 1e6 + i);
  return plugins
    .map((p, i) => ({ p, k: key(p.name, i) }))
    .sort((a, b) => a.k - b.k)
    .map((x) => x.p);
}

export function qamPlugins(plugins: LoadedPlugin[], settings: LoaderSettings, installedOrder: readonly { name: string }[] = []): { shown: LoadedPlugin[]; hidden: number } {
  const hidden = new Set(settings.hiddenPlugins);
  const withContent = plugins.filter((p) => p.content !== undefined && p.content !== null);
  const shown = orderPlugins(
    withContent.filter((p) => !hidden.has(p.name)),
    settings,
    installedOrder,
  );
  return { shown, hidden: withContent.length - shown.length };
}
