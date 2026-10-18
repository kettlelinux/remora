// The file picker plugins open with openFilePicker: a Steam modal over utilities/filepicker_ls.
import { useEffect, useRef, useState } from 'react';
import { DialogButton, Focusable, ModalRoot, Spinner, ToggleField } from '../../hooks/components';
import type { Backend } from '../../runtime/backend';
import { errorText } from '../../util/errors';
import { t } from '../i18n';
import { FileIcon, FolderIcon, UpIcon } from '../icons';

export interface FileEntry {
  isdir: boolean;
  name: string;
  realpath: string;
  size: number;
  modified: number;
  created: number;
}

interface Listing {
  realpath: string;
  files: FileEntry[];
  total: number;
  /** Entries fetched so far, before the client-side filter (what `total` counts). */
  loaded: number;
}

export interface FilePickerOptions {
  folderMode: boolean;
  startPath?: string;
  includeFiles: boolean;
  includeFolders: boolean;
  filter?: RegExp | ((file: FileEntry) => boolean);
  extensions?: string[];
  showHiddenFiles: boolean;
  allowAllFiles: boolean;
  max: number;
}

interface Props {
  backend: Backend;
  options: FilePickerOptions;
  onPick: (res: { path: string; realpath: string }) => void;
  /** Called on unmount when no ClosedPromise tells the caller the modal went away. */
  onUnmount?: () => void;
  closeModal?: () => void;
}

const join = (dir: string, name: string) => `${dir.replace(/\/+$/, '')}/${name}`;
const parent = (p: string) => p.replace(/\/+$/, '').replace(/\/[^/]*$/, '') || '/';

export function FilePickerModal({ backend, options, onPick, onUnmount, closeModal }: Props) {
  const [path, setPath] = useState<string | undefined>(options.startPath);
  const [listing, setListing] = useState<Listing>();
  const [page, setPage] = useState(1);
  const [error, setError] = useState<string>();
  const [hidden, setHidden] = useState(options.showHiddenFiles);
  const [allFiles, setAllFiles] = useState(false);
  /** Only the newest request may write state; older answers are dropped. */
  const seq = useRef(0);
  /** The path the first (no start path) load resolved to: setting it must not load it again. */
  const resolved = useRef<string | undefined>(undefined);
  const unmount = useRef(onUnmount);
  unmount.current = onUnmount;
  useEffect(() => () => unmount.current?.(), []);

  const load = async (p: string | undefined, pg: number) => {
    const my = ++seq.current;
    setError(undefined);
    try {
      const ext = allFiles || !options.extensions?.length ? null : options.extensions;
      const r = await backend.call<Listing>(
        'utilities/filepicker_ls',
        p ?? null,
        options.includeFiles,
        options.includeFolders,
        ext,
        hidden,
        'name_asc',
        null, // filters run here, so they never hide folders

        pg,
        options.max,
      );
      if (my !== seq.current) return;
      const f = options.filter;
      const fn = f instanceof RegExp ? (e: FileEntry) => e.name.search(f) >= 0 : f;
      const files = fn ? r.files.filter((f) => f.isdir || safeTest(fn, f)) : r.files;
      setListing((prev) =>
        pg > 1 && prev ? { ...r, files: [...prev.files, ...files], loaded: prev.loaded + r.files.length } : { ...r, files, loaded: r.files.length },
      );
      if (p === undefined) {
        resolved.current = r.realpath;
        setPath(r.realpath);
      }
    } catch (e) {
      if (my === seq.current) setError(errorText(e));
    }
  };

  useEffect(() => {
    setPage(1);
    if (path !== undefined && path === resolved.current) {
      resolved.current = undefined; // already loaded by the request that found it
      return;
    }
    resolved.current = undefined;
    void load(path, 1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, hidden, allFiles]);

  const current = path ?? listing?.realpath ?? '/';
  const pick = (entry?: FileEntry) => {
    onPick(entry ? { path: join(current, entry.name), realpath: entry.realpath } : { path: current, realpath: listing?.realpath ?? current });
    closeModal?.();
  };

  return (
    <ModalRoot closeModal={closeModal} onCancel={closeModal}>
      <h2 style={{ margin: '0 0 8px' }}>{options.folderMode ? t('picker.titleFolder') : t('picker.title')}</h2>
      <Focusable style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 8 }} flow-children="horizontal">
        <DialogButton style={{ width: 'auto', minWidth: 0, padding: '6px 12px' }} onClick={() => setPath(parent(current))}>
          <UpIcon size={16} />
        </DialogButton>
        <div style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontFamily: 'monospace' }}>{current}</div>
        {options.folderMode && (
          <DialogButton style={{ width: 'auto' }} onClick={() => pick()}>
            {t('picker.select')}
          </DialogButton>
        )}
      </Focusable>
      <ToggleField label={t('picker.showHidden')} checked={hidden} onChange={setHidden} bottomSeparator="none" />
      {options.allowAllFiles && options.extensions?.length ? (
        <ToggleField label={t('picker.allFiles')} checked={allFiles} onChange={setAllFiles} bottomSeparator="none" />
      ) : null}
      {error && <div style={{ color: '#ff6b6b', margin: '8px 0' }}>{error}</div>}
      {!listing && !error && <Spinner style={{ width: 32, height: 32 }} />}
      <Focusable style={{ maxHeight: '50vh', overflowY: 'auto' }} flow-children="vertical">
        {listing?.files.length === 0 && <div style={{ opacity: 0.7 }}>{t('picker.empty')}</div>}
        {listing?.files.map((f) => (
          <DialogButton
            key={f.realpath + f.name}
            style={{ display: 'flex', gap: 8, alignItems: 'center', textAlign: 'left', marginBottom: 4 }}
            onClick={() => (f.isdir ? setPath(join(current, f.name)) : !options.folderMode && pick(f))}
          >
            {f.isdir ? <FolderIcon size={18} /> : <FileIcon size={18} />}
            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{f.name}</span>
          </DialogButton>
        ))}
        {listing && listing.loaded < listing.total && (
          <DialogButton
            onClick={() => {
              const next = page + 1;
              setPage(next);
              void load(path, next);
            }}
          >
            {t('picker.more')}
          </DialogButton>
        )}
      </Focusable>
    </ModalRoot>
  );
}

function safeTest(fn: (f: FileEntry) => boolean, f: FileEntry): boolean {
  try {
    return Boolean(fn(f));
  } catch {
    return true;
  }
}
