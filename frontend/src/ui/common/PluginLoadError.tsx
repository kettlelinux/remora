// Panel of the placeholder entry shown for a plugin that failed to load.
import { PanelSection, PanelSectionRow, ButtonItem } from '../../hooks/components';
import { errorStack, errorText } from '../../util/errors';
import { t } from '../i18n';

export function PluginLoadError({ name, error }: { name: string; error: unknown }) {
  return (
    <PanelSection title={t('plugin.errorTitle', { name })}>
      <PanelSectionRow>
        <div style={{ fontSize: 13, marginBottom: 8 }}>{errorText(error)}</div>
      </PanelSectionRow>
      <PanelSectionRow>
        <pre style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontSize: 11, maxHeight: 300, overflowY: 'auto' }}>{errorStack(error)}</pre>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" onClick={() => void window.DeckyPluginLoader?.reloadPlugin(name).catch(() => {})}>
          {t('common.retry')}
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
}
