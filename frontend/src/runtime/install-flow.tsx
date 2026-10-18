// The install handshake: the backend asks for confirmation (loader/add_plugin_install_prompt),
// for root approval (remora/root_prompt), and reports progress and failures. Prompts show as
// Steam modals; if modals are unavailable they wait in `prompts` and render inline (QAM panel
// and store page), so an install never gets stuck without a way to answer.
//
// Root approval needs more than a click (Steam's UI can be driven by any local process): after
// Allow, the backend waits for a physical confirm (hold Volume Up + Volume Down) or a terminal
// command, and says so with remora/confirm_waiting and remora/confirm_done. Remora shows that
// wait as a modal (and inline) with the hint and a countdown; Cancel only closes the UI.
import type { ShowModalResult } from '@decky/ui';
import { useEffect, useState } from 'react';
import { modalsUsable, showModal } from '../hooks/modal';
import type { Toaster } from '../hooks/toaster';
import { ConfirmModal, DialogButton, Focusable, ModalRoot } from '../hooks/components';
import { compatText, isBlocked, type Compat } from '../ui/common/compat';
import { t } from '../ui/i18n';
import { WarningIcon } from '../ui/icons';
import { useStore } from '../ui/react-utils';
import { logger } from '../util/logger';
import { Store } from '../util/store';
import type { Backend } from './backend';

const log = logger('install');
const ROOT_PROMPT_MS = 5 * 60 * 1000;
/** How long past `until` the confirm UI stays up when confirm_done never arrives. */
const CONFIRM_GRACE_MS = 10_000;

export interface InstallRequest {
  name: string;
  version: string;
  hash?: string;
  install_type: number;
}

export type Prompt =
  | { kind: 'install'; requestId: string; requests: InstallRequest[] }
  | { kind: 'root'; requestId: string; name: string; version: string; mode?: string; hint?: string };

/** What the store knows about a requested zip (remora/install_request). */
export interface RequestDetail {
  name: string;
  version: string;
  hash?: string;
  install_type: number;
  source: string | null;
  /** 'self': signed with keys the index itself lists, so not verified. */
  signed: boolean | 'invalid' | 'self';
  compatible: boolean;
  root: boolean | null;
  /** Remora's compat answer for this version (older backends: absent). */
  compat?: Compat | null;
}

export interface Progress {
  name: string;
  percent: number;
  message?: string;
}

/** A root approval waiting for the physical (or terminal) confirm. `until` is epoch ms. */
export interface ConfirmWait {
  requestId: string | null;
  name: string;
  mode: string;
  hint: string;
  until: number;
}

export class InstallFlow {
  readonly prompts = new Store<Prompt[]>([]);
  readonly progress = new Store<Progress | null>(null);
  readonly details = new Store<Record<string, RequestDetail[]>>({});
  readonly confirm = new Store<ConfirmWait | null>(null);
  private failed = new Set<string>();
  private offs: (() => void)[] = [];
  private modals = new Map<Prompt, ShowModalResult>();
  private expiry = new Map<Prompt, ReturnType<typeof setTimeout>>();
  private confirmModal?: ShowModalResult;
  private confirmTimer?: ReturnType<typeof setTimeout>;

  constructor(
    private backend: Backend,
    private toaster: Toaster,
    private onFinished: (name: string) => void,
  ) {}

  listen(): void {
    const on = (event: string, fn: (...a: unknown[]) => void) => {
      this.backend.addEventListener(event, fn);
      this.offs.push(() => this.backend.removeEventListener(event, fn));
    };
    on('loader/add_plugin_install_prompt', (name, version, requestId, hash, installType) =>
      this.ask({ kind: 'install', requestId: String(requestId), requests: [{ name: String(name), version: String(version), hash: hash as string, install_type: Number(installType) || 0 }] }),
    );
    on('loader/add_multiple_plugins_install_prompt', (requestId, requests) =>
      this.ask({ kind: 'install', requestId: String(requestId), requests: Array.isArray(requests) ? (requests as InstallRequest[]) : [] }),
    );
    on('remora/install_request', (requestId, list) => {
      if (Array.isArray(list)) this.details.update((d) => ({ ...d, [String(requestId)]: list as RequestDetail[] }));
    });
    on('remora/root_prompt', (requestId, name, version, mode, hint) =>
      this.ask({
        kind: 'root',
        requestId: String(requestId),
        name: String(name),
        version: String(version),
        mode: typeof mode === 'string' ? mode : undefined,
        hint: typeof hint === 'string' ? hint : undefined,
      }),
    );
    on('remora/confirm_waiting', (requestId, name, mode, hint, until) => {
      const untilMs = Number(until) > 0 ? Number(until) * 1000 : Date.now() + 30_000;
      this.showConfirm({ requestId: requestId == null ? null : String(requestId), name: String(name), mode: String(mode ?? ''), hint: String(hint ?? ''), until: untilMs });
    });
    on('remora/confirm_done', (_requestId, name, ok) => {
      this.closeConfirm();
      this.toaster.toast({
        title: ok ? t('confirm.approved', { name: String(name) }) : t('confirm.notApproved', { name: String(name) }),
        body: ok ? '' : t('confirm.notApprovedBody'),
        logo: ok ? undefined : <WarningIcon />,
      });
    });
    on('loader/plugin_download_start', (name) => {
      this.failed.delete(String(name));
      this.progress.set({ name: String(name), percent: 0 });
    });
    on('loader/plugin_download_info', (percent, messageKey) => {
      const cur = this.progress.get();
      this.progress.set({ name: cur?.name ?? '', percent: Number(percent) || 0, message: messageKey ? String(messageKey) : undefined });
    });
    // download_finish also follows a failure (after remora/install_failed).
    on('loader/plugin_download_finish', (name) => {
      this.progress.set(null);
      const n = String(name);
      if (this.failed.delete(n)) return;
      this.toaster.toast({ title: t('install.done', { name: n }), body: '' });
      this.onFinished(n);
    });
    // developer mode installed something the compat check rules out
    on('remora/install_warning', (name, message) => {
      this.toaster.toast({ title: t('install.warning', { name: String(name) }), body: String(message ?? ''), logo: <WarningIcon /> });
    });
    on('remora/install_failed', (name, message) => {
      this.failed.add(String(name));
      this.progress.set(null);
      this.toaster.toast({ title: t('install.failed', { name: String(name) }), body: String(message ?? '') });
    });
  }

  private ask(p: Prompt): void {
    for (const old of this.prompts.get()) if (old.requestId === p.requestId && old.kind === p.kind) this.drop(old, true, false);
    this.prompts.update((all) => [...all, p]);
    // The backend cancels an unanswered root prompt after 5 minutes; drop it (and its modal) here too.
    if (p.kind === 'root') this.expiry.set(p, setTimeout(() => this.drop(p, true), ROOT_PROMPT_MS));
    if (!modalsUsable()) return; // answered inline
    const result = showModal(<PromptModal prompt={p} flow={this} answer={(ok) => void this.answer(p, ok, true)} />, promptTitle(p));
    if (!result) {
      log.warn('modal failed; prompt waits inline');
      return;
    }
    this.modals.set(p, result);
    void result.ClosedPromise?.then(() => {
      if (this.modals.get(p) === result) this.modals.delete(p);
    });
  }

  /** Takes a prompt out of the list, stops its expiry timer and (unless it closed itself) its modal. */
  private drop(p: Prompt, closeModal: boolean, dropDetails = true): void {
    clearTimeout(this.expiry.get(p));
    this.expiry.delete(p);
    this.prompts.update((all) => all.filter((x) => x !== p));
    const modal = this.modals.get(p);
    this.modals.delete(p);
    if (modal && closeModal) safeClose(modal);
    if (dropDetails) {
      this.details.update((d) => {
        if (!(p.requestId in d)) return d;
        const { [p.requestId]: _drop, ...rest } = d;
        return rest;
      });
    }
  }

  /** `fromModal`: the answer came from the prompt's own modal, which closes itself. */
  async answer(p: Prompt, ok: boolean, fromModal = false): Promise<void> {
    if (!this.prompts.get().includes(p)) return; // already answered elsewhere
    this.drop(p, !fromModal);
    try {
      if (p.kind === 'root') await this.backend.call('remora/store/answer_root', p.requestId, ok);
      else await this.backend.call(ok ? 'utilities/confirm_plugin_install' : 'utilities/cancel_plugin_install', p.requestId);
    } catch (e) {
      log.error('answering the prompt failed', e);
    }
  }

  private showConfirm(c: ConfirmWait): void {
    this.closeConfirm();
    this.confirm.set(c);
    // confirm_done can be lost in a socket gap: give up a little after the deadline.
    this.confirmTimer = setTimeout(() => this.closeConfirm(), Math.min(Math.max(0, c.until - Date.now()) + CONFIRM_GRACE_MS, ROOT_PROMPT_MS + CONFIRM_GRACE_MS));
    if (!modalsUsable()) return; // shown inline
    const modal = showModal(<ConfirmWaitModal wait={c} cancel={() => this.closeConfirm()} />, t('confirm.title', { name: c.name }));
    if (!modal) return;
    this.confirmModal = modal;
    void modal.ClosedPromise?.then(() => {
      // closed with B: that is a Cancel too (the backend keeps waiting until its timeout)
      if (this.confirmModal === modal) {
        this.confirmModal = undefined;
        this.closeConfirm();
      }
    });
  }

  /** Closes the confirm UI only; the backend keeps waiting until it times out. */
  closeConfirm(): void {
    clearTimeout(this.confirmTimer);
    this.confirmTimer = undefined;
    this.confirm.set(null);
    const modal = this.confirmModal;
    this.confirmModal = undefined;
    if (modal) safeClose(modal);
  }

  deinit(): void {
    for (const off of this.offs) off();
    this.offs = [];
    for (const p of [...this.prompts.get()]) this.drop(p, true);
    this.closeConfirm();
  }
}

function safeClose(modal: ShowModalResult): void {
  try {
    modal.Close();
  } catch (e) {
    log.debug('closing a modal failed', e);
  }
}

/** Whole seconds left until `until` (epoch ms), ticking once a second. */
export function useSecondsLeft(until: number): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [until]);
  return Math.max(0, Math.ceil((until - now) / 1000));
}

export function confirmHint(mode: string | undefined, hint: string | undefined, name: string): string {
  if (hint) return hint;
  return mode === 'terminal' ? t('confirm.hintTerminal', { name }) : t('confirm.hintKeys');
}

function ConfirmWaitModal({ wait, cancel, closeModal }: { wait: ConfirmWait; cancel: () => void; closeModal?: () => void }) {
  const left = useSecondsLeft(wait.until);
  return (
    <ModalRoot closeModal={closeModal} onCancel={cancel}>
      <h2 style={{ margin: '0 0 8px' }}>{t('confirm.title', { name: wait.name })}</h2>
      <ConfirmWaitBody wait={wait} left={left} />
      <Focusable style={{ display: 'flex', marginTop: 16 }} flow-children="horizontal">
        <DialogButton onClick={cancel} preferredFocus {...({ autoFocus: true } as object)}>
          {t('common.cancel')}
        </DialogButton>
      </Focusable>
    </ModalRoot>
  );
}

export function ConfirmWaitBody({ wait, left }: { wait: ConfirmWait; left: number }) {
  return (
    <>
      <div style={{ fontSize: 13, opacity: 0.8 }}>{t('confirm.intro')}</div>
      <div style={{ fontSize: 20, fontWeight: 700, margin: '12px 0', whiteSpace: 'pre-wrap' }}>{confirmHint(wait.mode, wait.hint, wait.name)}</div>
      <div style={{ fontSize: 13, opacity: 0.8 }}>{left > 0 ? t('confirm.countdown', { seconds: left }) : t('confirm.expired')}</div>
      <div style={{ fontSize: 11, opacity: 0.6, marginTop: 8 }}>{t('confirm.cancelNote')}</div>
    </>
  );
}

export function promptTitle(p: Prompt): string {
  if (p.kind === 'root') return t('root.title', { name: p.name });
  if (p.requests.length === 1) {
    const r = p.requests[0]!;
    const key = `install.title.${Math.min(Math.max(r.install_type, 0), 4)}` as 'install.title.0';
    return t(key, { name: r.name });
  }
  return t('install.multiTitle', { count: p.requests.length });
}

export function promptBody(p: Prompt, details?: RequestDetail[]): string {
  if (p.kind === 'root') {
    const how = p.mode === 'terminal' ? t('root.allowTerminal') : t('root.allowKeys');
    return `${t('root.body')}\n\n${how}\n${confirmHint(p.mode, p.hint, p.name)}`;
  }
  return p.requests
    .map((r) => {
      const d = details?.find((x) => x.name === r.name);
      const lines = [`${r.name}: ${t('install.body', { version: r.version })}`];
      if (d) {
        lines.push(d.source ? t('install.fromSource', { source: d.source }) : t('install.noSource'));
        if (d.signed === true) lines.push(t('install.signed'));
        else if (d.signed === 'invalid') lines.push(t('install.badSignature'));
        else if (d.signed === 'self') lines.push(t('install.unsigned'), t('install.selfSigned'));
        else if (d.source) lines.push(t('install.unsigned'));
        if (!d.compatible) lines.push(t('install.incompatible'));
        if (isBlocked(d.compat)) lines.push(t('install.compatBlocked'), compatText(d.compat) ?? '');
        if (d.root) lines.push(t('install.root'));
      }
      return lines.join('\n');
    })
    .join('\n\n');
}

/** Progress text for loader/plugin_download_info message keys (Remora.install.<step>). */
export function progressText(p: Progress): string {
  const step = p.message?.split('.').pop();
  const known = ['downloading', 'verifying', 'unpacking', 'binaries', 'installing'];
  const name = t('install.progress', { name: p.name });
  return step && known.includes(step) ? `${name}: ${t(`install.step.${step}` as 'install.step.downloading')}` : name;
}

function PromptModal({ prompt, flow, answer, closeModal }: { prompt: Prompt; flow: InstallFlow; answer: (ok: boolean) => void; closeModal?: () => void }) {
  const root = prompt.kind === 'root';
  // remora/install_request usually arrives just after the prompt; the text updates when it does.
  const details = useStore(flow.details)[prompt.requestId];
  return (
    <ConfirmModal
      strTitle={promptTitle(prompt)}
      strDescription={<div style={{ whiteSpace: 'pre-wrap' }}>{promptBody(prompt, details)}</div>}
      strOKButtonText={root ? t('root.allow') : t('common.confirm')}
      strCancelButtonText={root ? t('root.deny') : t('common.cancel')}
      bDestructiveWarning={root}
      onOK={() => answer(true)}
      onCancel={() => answer(false)}
      closeModal={closeModal}
    />
  );
}
