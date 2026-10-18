// /decky/settings (and /remora/settings): Remora's settings, one sidebar page per area.
import type { ReactNode } from 'react';
import { SidebarNavigation } from '../../hooks/components';
import type { PluginLoader } from '../../runtime/plugin-loader';
import { PluginBoundary } from '../../hooks/error-boundary';
import { t } from '../i18n';
import { GearIcon, PlugIcon, RemoraIcon, StoreIcon, WarningIcon } from '../icons';
import { AboutPage } from './AboutPage';
import { DeveloperPage } from './DeveloperPage';
import { HooksPage } from './HooksPage';
import { PermissionsPage } from './PermissionsPage';
import { PluginsPage } from './PluginsPage';
import { SourcesPage } from './SourcesPage';

export function settingsPage(loader: PluginLoader, base: string) {
  return function RemoraSettings() {
    const page = (content: ReactNode) => <PluginBoundary>{content}</PluginBoundary>;
    return (
      <SidebarNavigation
        title={t('settings.title')}
        showTitle
        pages={[
          { title: t('settings.plugins'), icon: <PlugIcon />, route: `${base}/plugins`, content: page(<PluginsPage loader={loader} />) },
          { title: t('settings.permissions'), icon: <WarningIcon />, route: `${base}/permissions`, content: page(<PermissionsPage loader={loader} />) },
          { title: t('settings.sources'), icon: <StoreIcon />, route: `${base}/sources`, content: page(<SourcesPage loader={loader} />) },
          { title: t('settings.hooks'), icon: <GearIcon />, route: `${base}/hooks`, content: page(<HooksPage />) },
          { title: t('settings.developer'), icon: <GearIcon />, route: `${base}/developer`, content: page(<DeveloperPage loader={loader} />) },
          { title: t('settings.about'), icon: <RemoraIcon />, route: `${base}/about`, content: page(<AboutPage loader={loader} />) },
        ]}
      />
    );
  };
}
