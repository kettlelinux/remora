// openFilePicker (modern) and the legacy (startPath, selectFiles, regex) form.
import { showModal } from '../hooks/modal';
import type { Backend } from '../runtime/backend';
import { t } from '../ui/i18n';
import { FilePickerModal, type FileEntry } from '../ui/modals/FilePickerModal';

export interface FilePickerRes {
  path: string;
  realpath: string;
}

export const FILE = 0;
export const FOLDER = 1;

export function filePickerApi(backend: Backend) {
  const openFilePicker = (
    select: number,
    startPath?: string,
    includeFiles?: boolean,
    includeFolders = true,
    filter?: RegExp | ((file: FileEntry) => boolean),
    extensions?: string[],
    showHiddenFiles = false,
    allowAllFiles = true,
    max = 1000,
  ): Promise<FilePickerRes> =>
    new Promise<FilePickerRes>((resolve, reject) => {
      const folderMode = select === FOLDER;
      let picked = false;
      const modal = showModal(
        <FilePickerModal
          backend={backend}
          options={{
            folderMode,
            startPath: startPath || undefined,
            includeFiles: includeFiles ?? !folderMode,
            includeFolders,
            filter,
            extensions,
            showHiddenFiles,
            allowAllFiles,
            max,
          }}
          onPick={(res) => {
            picked = true;
            resolve(res);
          }}
        />,
        folderMode ? t('picker.titleFolder') : t('picker.title'),
      );
      if (!modal) {
        reject(new Error(t('picker.unavailable')));
        return;
      }
      void modal.ClosedPromise?.then(() => {
        if (!picked) reject('User canceled');
      });
    });

  /** Legacy ServerAPI form: selectFiles true = FILE mode (files and folders), false = folders only. */
  const openFilePickerLegacy = (startPath: string, selectFiles = true, regex?: RegExp): Promise<FilePickerRes> =>
    selectFiles ? openFilePicker(FILE, startPath, true, true, regex) : openFilePicker(FOLDER, startPath, false, true, regex);

  return { openFilePicker, openFilePickerLegacy };
}
