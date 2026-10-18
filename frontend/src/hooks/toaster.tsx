// Lookup 7: toasts. Two parts with separate status:
//  - toast-renderer: Valve's notification renderer, patched (through @decky/ui's FC trampoline)
//    so notifications Remora posted render Remora's toast component.
//  - toast-store:    NotificationStore.ProcessNotification, which posts them.
// Toasts are only posted when both work; otherwise toast() logs and returns a no-op handle.
import { findModuleExport, injectFCTrampoline } from '@decky/ui';
import type { ReactNode } from 'react';
import { notificationStore, SteamAny } from '../steam/globals';
import { logger } from '../util/logger';
import { RemoraToast } from '../ui/common/Toast';
import { PluginBoundary } from './error-boundary';
import { typeSource } from './fiber';
import { HookHandle, runStrategies } from './status';

const rendererHook = new HookHandle('toast-renderer', 'toasts (plugins\' toaster.toast calls are logged instead)');
const storeHook = new HookHandle('toast-store', 'toasts (plugins\' toaster.toast calls are logged instead)');
const log = logger('toaster');

const RENDERER_MARK = 'controller:"notification",method:';

export interface ToastData {
  title: ReactNode;
  body: ReactNode;
  subtext?: ReactNode;
  logo?: ReactNode;
  icon?: ReactNode;
  timestamp?: Date;
  onClick?: () => void;
  className?: string;
  contentClassName?: string;
  duration?: number;
  expiration?: number;
  critical?: boolean;
  eType?: number;
  sound?: number;
  showNewIndicator?: boolean;
  playSound?: boolean;
  showToast?: boolean;
}

export interface ToastNotification {
  data: ToastData;
  dismiss: () => void;
}

const ours = new WeakSet<object>();

/** Finds the notification Remora posted in the renderer's props, if this render is one. */
function ourToast(props: SteamAny): { data: ToastData } | undefined {
  const candidates = [props?.notification, props?.toast, props?.group?.notifications?.[0], props?.notifications?.[0], props];
  for (const c of candidates) {
    if (!c || typeof c !== 'object') continue;
    if ((c.remora || c.decky) && c.data) return c;
    if (c.data && typeof c.data === 'object' && ours.has(c.data)) return c;
  }
  return undefined;
}

export class Toaster {
  private trampoline?: { component: SteamAny };
  private originalComponent?: SteamAny;

  constructor() {
    window.__TOASTER_INSTANCE?.deinit?.();
    window.__TOASTER_INSTANCE = this;
  }

  init(): void {
    const found = runStrategies<SteamAny>(rendererHook, [
      { name: 'function with notification controller source', find: () => findModuleExport((e: SteamAny) => typeof e === 'function' && typeSource(e).includes(RENDERER_MARK)) },
      {
        name: 'memo with notification controller source',
        find: () => findModuleExport((e: SteamAny) => e && typeof e === 'object' && typeof e.type === 'function' && typeSource(e.type).includes(RENDERER_MARK))?.type,
      },
    ], false);
    if (!found) {
      rendererHook.fail('Valve toast renderer not found');
    } else {
      try {
        const t = injectFCTrampoline(found.value);
        const original = t.component;
        this.trampoline = t as SteamAny;
        this.originalComponent = original;
        t.component = function (this: unknown, props: SteamAny, ...rest: unknown[]) {
          const toast = ourToast(props);
          if (!toast) return (original as SteamAny).call(this, props, ...rest);
          return (
            <PluginBoundary fallback={null}>
              <RemoraToast data={toast.data} location={props?.location} />
            </PluginBoundary>
          );
        } as SteamAny;
        rendererHook.ok(found.strategy);
      } catch (e) {
        rendererHook.fail(`trampoline failed: ${String(e)}`);
      }
    }
    const s = notificationStore();
    if (typeof s?.ProcessNotification === 'function') storeHook.ok('NotificationStore.ProcessNotification');
    else storeHook.fail('NotificationStore.ProcessNotification missing');
  }

  toast = (data: ToastData): ToastNotification => {
    // Defaults are written back into the caller's object, as plugins expect.
    if (!(data.timestamp instanceof Date)) data.timestamp = data.timestamp ? new Date(data.timestamp as unknown as number) : new Date();
    data.duration ??= 5000;
    data.eType ??= 31;
    data.sound ??= 6;
    data.showNewIndicator ??= true;
    data.playSound ??= true;
    data.showToast ??= true;
    const handle: ToastNotification = { data, dismiss: () => {} };

    const store = notificationStore();
    if (!this.trampoline || typeof store?.ProcessNotification !== 'function') {
      log.info('toast (not shown, toasts are off):', data.title, data.body);
      return handle;
    }
    ours.add(data);
    let group: SteamAny;
    try {
      const id = typeof store.m_nNextTestNotificationID === 'number' ? store.m_nNextTestNotificationID++ : Date.now();
      // ProcessNotification(config, notification, update): the config says how to show it (the
      // per-type entry Steam keeps for its own notifications), the notification is what goes into
      // the toast queue and the tray, carrying the ToastData as `data`. The ToastData itself must
      // not be the notification: the tray is observable, and `data.data = data` recursed forever.
      const notification = {
        notificationID: id,
        rtCreated: Math.floor(data.timestamp.getTime() / 1000),
        eType: data.eType,
        eSource: 1,
        nToastDurationMS: data.duration,
        bNewIndicator: data.showNewIndicator,
        data,
        decky: true,
        remora: true,
      };
      const config = {
        showToast: data.showToast,
        bCritical: data.critical,
        bEnableInReducedUI: true,
        toastDurationMS: data.duration,
        playSound: data.playSound,
        sound: data.sound,
        fnTray: (toast: SteamAny, tray: SteamAny[]) => {
          group = { eType: data.eType, notifications: [toast] };
          tray.unshift(group);
        },
      };
      store.ProcessNotification(config, notification, 0);
    } catch (e) {
      storeHook.fail(`ProcessNotification threw: ${String(e)}`);
      return handle;
    }
    handle.dismiss = () => {
      try {
        if (group) store.RemoveGroupFromTray?.(group);
      } catch (e) {
        log.warn('dismiss failed', e);
      }
    };
    if (data.expiration && data.expiration > 0) setTimeout(handle.dismiss, data.expiration);
    return handle;
  };

  deinit(): void {
    if (this.trampoline && this.originalComponent) this.trampoline.component = this.originalComponent;
    this.trampoline = undefined;
  }
}
