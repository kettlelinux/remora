// Install progress, prompts waiting for an answer, and a root confirm in progress. Prompts and
// the confirm also open as modals; this inline copy is the way to answer when Steam's modals are
// not available.
import { ButtonItem, PanelSection, PanelSectionRow, ProgressBarWithInfo } from '../../hooks/components';
import { ConfirmWaitBody, progressText, promptBody, promptTitle, useSecondsLeft, type ConfirmWait, type InstallFlow } from '../../runtime/install-flow';
import { t } from '../i18n';
import { useStore } from '../react-utils';

export function PendingPrompts({ flow }: { flow: InstallFlow }) {
  const prompts = useStore(flow.prompts);
  const progress = useStore(flow.progress);
  const details = useStore(flow.details);
  const confirm = useStore(flow.confirm);
  if (!prompts.length && !progress && !confirm) return null;
  return (
    <PanelSection title={t('prompts.pending')}>
      {confirm && <ConfirmCard flow={flow} wait={confirm} />}
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

function ConfirmCard({ flow, wait }: { flow: InstallFlow; wait: ConfirmWait }) {
  const left = useSecondsLeft(wait.until);
  return (
    <PanelSectionRow>
      <div style={{ fontWeight: 700 }}>{t('confirm.title', { name: wait.name })}</div>
      <ConfirmWaitBody wait={wait} left={left} />
      <ButtonItem layout="below" onClick={() => flow.closeConfirm()}>
        {t('common.cancel')}
      </ButtonItem>
    </PanelSectionRow>
  );
}
