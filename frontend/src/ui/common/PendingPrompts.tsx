// Install progress and prompts waiting for an answer. Prompts also open as modals; this inline
// copy is the way to answer when Steam's modals are not available.
import { ButtonItem, PanelSection, PanelSectionRow, ProgressBarWithInfo } from '../../hooks/components';
import { progressText, promptBody, promptTitle, type InstallFlow } from '../../runtime/install-flow';
import { t } from '../i18n';
import { useStore } from '../react-utils';

export function PendingPrompts({ flow }: { flow: InstallFlow }) {
  const prompts = useStore(flow.prompts);
  const progress = useStore(flow.progress);
  const details = useStore(flow.details);
  if (!prompts.length && !progress) return null;
  return (
    <PanelSection title={t('prompts.pending')}>
      {progress && (
        <PanelSectionRow>
          <ProgressBarWithInfo nProgress={progress.percent} sOperationText={progressText(progress)} indeterminate={progress.percent <= 0} />
        </PanelSectionRow>
      )}
      {prompts.map((p) => (
          <PanelSectionRow key={`${p.kind}:${p.requestId}`}>
            <div style={{ fontWeight: 700 }}>{promptTitle(p)}</div>
            <div style={{ fontSize: 12, whiteSpace: 'pre-wrap', opacity: 0.8 }}>{promptBody(p, details[p.requestId])}</div>
            <ButtonItem layout="below" onClick={() => void flow.answer(p, true)}>
              {p.kind === 'root' ? t('root.allow') : t('common.confirm')}
            </ButtonItem>
            <ButtonItem layout="below" onClick={() => void flow.answer(p, false)}>
              {p.kind === 'root' ? t('root.deny') : t('common.cancel')}
            </ButtonItem>
          </PanelSectionRow>
      ))}
    </PanelSection>
  );
}
