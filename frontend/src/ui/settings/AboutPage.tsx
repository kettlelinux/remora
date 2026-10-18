import { useEffect, useState } from 'react';
import { Field, Focusable } from '../../hooks/components';
import type { PluginLoader } from '../../runtime/plugin-loader';
import { t } from '../i18n';
import { useStore } from '../react-utils';

export function AboutPage({ loader }: { loader: PluginLoader }) {
  const status = useStore(loader.status.loader);
  const [injector, setInjector] = useState<string>();
  useEffect(() => {
    loader.backend.call<string>('remora/injector_status').then(setInjector, () => setInjector(undefined));
  }, []);
  return (
    <Focusable>
      <Field label={t('about.version')} focusable>
        {status?.version ?? '?'}
      </Field>
      <Field label={t('about.deckyVersion')} focusable>
        {status?.deckyVersion ?? '?'}
      </Field>
      {injector !== undefined && (
        <Field label={t('about.injector')} focusable>
          {injector}
        </Field>
      )}
      <p style={{ opacity: 0.7, fontSize: 12 }}>{t('about.license')}</p>
    </Focusable>
  );
}
