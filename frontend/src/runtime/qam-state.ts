// State of Remora's Quick Access tab: whether the QAM is visible and which plugin is open in it.
import { Store } from '../util/store';

export const qamVisible = new Store<boolean>(false);
export const activePlugin = new Store<string | null>(null);

// Steam's QAM component passed `visible` as a render argument in the builds Decky was written
// against; the current one doesn't. Until it shows up, the QAM window's own document visibility
// decides (reportQamDocumentVisible).
let renderArgSeen = false;

/** Steam calls this from render; listeners run after the render, never during it. */
export function reportQamVisible(visible: unknown): void {
  if (typeof visible !== 'boolean') return;
  renderArgSeen = true;
  if (visible === qamVisible.get()) return;
  queueMicrotask(() => qamVisible.set(visible));
}

/** From the panel: whether the document it is rendered in (the QAM window's) is visible. */
export function reportQamDocumentVisible(visible: boolean): void {
  if (renderArgSeen || visible === qamVisible.get()) return;
  queueMicrotask(() => qamVisible.set(visible));
}
