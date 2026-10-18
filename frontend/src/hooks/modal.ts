// Lookup 11: Steam's modal system (through @decky/ui's showModal). When it is missing, Remora's
// prompts (install, root, file picker) fall back to inline cards in the QAM panel and store.
import type { ReactNode } from 'react';
import { ConfirmModal, ModalRoot, showModal as dflShowModal, ShowModalResult } from '@decky/ui';
import { HookHandle } from './status';

const hook = new HookHandle('modals', 'pop-up dialogs (install and root prompts fall back to inline cards; file picker is off)');

export function checkModals(): void {
  // Found by @decky/ui heuristics, so they can be undefined despite their types.
  const confirm = ConfirmModal as unknown;
  const root = ModalRoot as unknown;
  if (confirm && root) hook.ok('@decky/ui ConfirmModal + ModalRoot');
  else hook.degraded('partial', `${confirm ? '' : 'ConfirmModal '}${root ? '' : 'ModalRoot '}not found`.trim());
}

export function modalsUsable(): boolean {
  return hook.state !== 'failed' && Boolean(ModalRoot);
}

/** Shows a modal, or returns undefined (and marks modals failed) if Steam's modal API is gone. */
export function showModal(node: ReactNode, title = 'Remora'): ShowModalResult | undefined {
  try {
    const r = dflShowModal(node, undefined, { strTitle: title, bHideMainWindowForPopouts: false });
    if (!r) throw new Error('showModal returned nothing');
    return r;
  } catch (e) {
    hook.fail(`showModal threw: ${e instanceof Error ? e.message : String(e)}`);
    return undefined;
  }
}
