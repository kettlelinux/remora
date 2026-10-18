// Developer options: logging, installs from a URL or zip, reload, disable all.
import { useState } from 'react';
import { ButtonItem, Focusable, PanelSection, PanelSectionRow, TextField, ToggleField } from '../../hooks/components';
import { filePickerApi, FILE } from '../../api/file-picker';
import type { PluginLoader } from '../../runtime/plugin-loader';
import { errorText } from '../../util/errors';
import { setVerbose } from '../../util/logger';
import { ConfirmButton } from '../common/ConfirmButton';
import { t } from '../i18n';
import { useStore } from '../react-utils';

export function DeveloperPage({ loader }: { loader: PluginLoader }) {
  const settings = useStore(loader.settings.values);
  const [url, setUrl] = useState('');
  const [error, setError] = useState<string>();
  const call = (route: string, ...args: unknown[]) => loader.backend.call(route, ...args).catch((e) => setError(errorText(e)));

  const installFrom = (artifact: string) => {
    const base = artifact.split(/[/\\]/).pop() ?? 'plugin';
    void call('utilities/install_plugin', artifact, decodeURIComponent(base.replace(/\.zip$/i, '')), 'dev', '', 0);
  };

  return (
    <Focusable>
      <ToggleField label={t('dev.mode')} description={t('dev.modeDesc')} checked={settings['developer.enabled']} onChange={(v) => void loader.settings.set('developer.enabled', v)} />
      {error && <p style={{ color: '#ff6b6b' }}>{error}</p>}
      {settings['developer.enabled'] && (
        <>
          <ToggleField
            label={t('dev.verbose')}
            description={t('dev.verboseDesc')}
            checked={settings['remora.verbose']}
            onChange={(v) => {
              setVerbose(v);
              void loader.settings.set('remora.verbose', v);
            }}
          />
          <PanelSection title={t('dev.installUrl')}>
            <PanelSectionRow>
              <TextField value={url} onChange={(e) => setUrl(e.currentTarget.value)} description={t('dev.thirdParty')} />
            </PanelSectionRow>
            <PanelSectionRow>
              <ButtonItem
                layout="below"
                disabled={!url.trim()}
                onClick={() => {
                  const u = url.trim();
                  installFrom(/^[a-z]+:\/\//i.test(u) ? u : `https://${u}`);
                }}
              >
                {t('dev.installUrlButton')}
              </ButtonItem>
            </PanelSectionRow>
          </PanelSection>
          <PanelSection title={t('dev.installZip')}>
            <PanelSectionRow>
              <ButtonItem
                layout="below"
                onClick={async () => {
                  try {
                    const home = await loader.backend.call<{ path?: string }>('utilities/get_user_info').catch(() => undefined);
                    const res = await filePickerApi(loader.backend).openFilePicker(FILE, home?.path, true, true, undefined, ['zip'], false, false);
                    installFrom(`file://${res.realpath}`);
                  } catch (e) {
                    if (e !== 'User canceled') setError(errorText(e));
                  }
                }}
              >
                {t('dev.installZipButton')}
              </ButtonItem>
            </PanelSectionRow>
          </PanelSection>
          <PanelSection>
            <PanelSectionRow>
              <ButtonItem layout="below" onClick={() => void call('utilities/restart_webhelper')}>
                {t('dev.reloadUI')}
              </ButtonItem>
            </PanelSectionRow>
            <PanelSectionRow>
              <ConfirmButton label={t('dev.disableAll')} confirmText={t('dev.disableAllConfirm')} onConfirm={() => void call('utilities/set_all_plugins_disabled')} />
            </PanelSectionRow>
          </PanelSection>
        </>
      )}
    </Focusable>
  );
}
