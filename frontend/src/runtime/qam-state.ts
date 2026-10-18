// State of Remora's Quick Access tab: whether the QAM is visible (from Steam's render argument)
// and which plugin is open in it.
import { Store } from '../util/store';

export const qamVisible = new Store<boolean>(false);
export const activePlugin = new Store<string | null>(null);

/** Steam calls this from render; listeners run after the render, never during it. */
export function reportQamVisible(visible: unknown): void {
  if (typeof visible !== 'boolean' || visible === qamVisible.get()) return;
  queueMicrotask(() => qamVisible.set(visible));
}
