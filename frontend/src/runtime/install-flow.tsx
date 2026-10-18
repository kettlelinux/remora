// The install handshake: the backend asks for confirmation (loader/add_plugin_install_prompt),
// for root approval (remora/root_prompt), and reports progress and failures. Prompts show as
// Steam modals; if modals are unavailable they wait in `prompts` and render inline (QAM panel
// and store page), so an install never gets stuck without a way to answer.
import { modalsUsable, showModal } from '../hooks/modal';
import type { Toaster } from '../hooks/toaster';
import { ConfirmModal } from '../hooks/components';
import { t } from '../ui/i18n';
import { useStore } from '../ui/react-utils';
import { logger } from '../util/logger';
import { Store } from '../util/store';
import type { Backend } from './backend';

const log = logger('install');
const ROOT_PROMPT_MS = 5 * 60 * 1000;

export interface InstallRequest {
  name: string;
  version: string;
  hash?: string;
  install_type: number;
}

export type Prompt =
  | { kind: 'install'; requestId: string; requests: InstallRequest[] }
  | { kind: 'root'; requestId: string; name: string; version: string };

/** What the store knows about a requested zip (remora/install_request). */
export interface RequestDetail {
  name: string;
  version: string;
  hash?: string;
  install_type: number;
  source: string | null;
  signed: boolean | 'invalid';
  compatible: boolean;
  root: boolean | null;
}

export interface Progress {
  name: string;
  percent: number;
  message?: string;
}

export class InstallFlow {
  readonly prompts = new Store<Prompt[]>([]);
  readonly progress = new Store<Progress | null>(null);
  readonly details = new Store<Record<string, RequestDetail[]>>({});
  private failed = new Set<string>();
  private offs: (() => void)[] = [];

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
    on('remora/root_prompt', (requestId, name, version) => this.ask({ kind: 'root', requestId: String(requestId), name: String(name), version: String(version) }));
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
    on('remora/install_failed', (name, message) => {
      this.failed.add(String(name));
      this.progress.set(null);
      this.toaster.toast({ title: t('install.failed', { name: String(name) }), body: String(message ?? '') });
    });
  }

  private ask(p: Prompt): void {
    this.prompts.update((all) => [...all.filter((x) => x.requestId !== p.requestId || x.kind !== p.kind), p]);
    // The backend cancels an unanswered root prompt after 5 minutes; drop it here too.
    if (p.kind === 'root') setTimeout(() => this.prompts.update((all) => all.filter((x) => x !== p)), ROOT_PROMPT_MS);
    if (!modalsUsable()) return; // answered inline
    const result = showModal(<PromptModal prompt={p} flow={this} answer={(ok) => void this.answer(p, ok)} />, promptTitle(p));
    if (!result) log.warn('modal failed; prompt waits inline');
  }

  async answer(p: Prompt, ok: boolean): Promise<void> {
    if (!this.prompts.get().includes(p)) return; // already answered elsewhere
    this.prompts.update((all) => all.filter((x) => x !== p));
    this.details.update((d) => {
      const { [p.requestId]: _drop, ...rest } = d;
      return rest;
    });
    try {
      if (p.kind === 'root') await this.backend.call('remora/store/answer_root', p.requestId, ok);
      else await this.backend.call(ok ? 'utilities/confirm_plugin_install' : 'utilities/cancel_plugin_install', p.requestId);
    } catch (e) {
      log.error('answering the prompt failed', e);
    }
  }

  deinit(): void {
    for (const off of this.offs) off();
    this.offs = [];
  }
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
  if (p.kind === 'root') return t('root.body');
  return p.requests
    .map((r) => {
      const d = details?.find((x) => x.name === r.name);
      const lines = [`${r.name}: ${t('install.body', { version: r.version })}`];
      if (d) {
        lines.push(d.source ? t('install.fromSource', { source: d.source }) : t('install.noSource'));
        if (d.signed === true) lines.push(t('install.signed'));
        else if (d.signed === 'invalid') lines.push(t('install.badSignature'));
        else if (d.source) lines.push(t('install.unsigned'));
        if (!d.compatible) lines.push(t('install.incompatible'));
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
