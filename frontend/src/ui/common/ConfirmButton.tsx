// A button that asks for a second press before it runs: works with the gamepad and needs no modal.
import { useEffect, useState, type ReactNode } from 'react';
import { ButtonItem } from '../../hooks/components';
import { t } from '../i18n';

export function ConfirmButton({ label, confirmText, description, onConfirm, disabled }: { label: ReactNode; confirmText: string; description?: ReactNode; onConfirm: () => void; disabled?: boolean }) {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const id = setTimeout(() => setArmed(false), 5000);
    return () => clearTimeout(id);
  }, [armed]);
  return (
    <ButtonItem
      layout="below"
      disabled={disabled}
      description={armed ? confirmText : description}
      onClick={() => {
        if (!armed) return setArmed(true);
        setArmed(false);
        onConfirm();
      }}
    >
      {armed ? `${t('common.confirm')}: ${typeof label === 'string' ? label : ''}` : label}
    </ButtonItem>
  );
}
