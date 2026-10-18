// WebSocket client for ws://127.0.0.1:1337/ws (docs/PROTOCOL.md, compat/frontend.md §4.2).
// Calls made while the socket is down wait in `pending` and go out in the FULL_SYNC sent on
// every (re)connect; the backend answers ids it already ran from its reply cache.
import { logger } from '../util/logger';
import { Store } from '../util/store';
import { currentToken } from './token';

const enum MessageType {
  ERROR = -1,
  CALL = 0,
  REPLY = 1,
  DISCARD = 2,
  RECEIVED_RESPONSE = 3,
  FULL_SYNC = 4,
  EVENT = 5,
}

interface CallMessage {
  type: MessageType.CALL;
  id: number;
  route: string;
  args: unknown[];
}

interface PendingCall {
  msg: CallMessage;
  resolve: (v: unknown) => void;
  reject: (e: unknown) => void;
}

interface PyErrorPayload {
  name?: string;
  message?: string;
  error?: string;
  traceback?: string | null;
}

export class PyError extends Error {
  pythonTraceback: string | null;
  constructor(err: PyErrorPayload) {
    super(err.message || err.error || '');
    this.name = `Python ${err.name ?? 'Error'}`;
    this.pythonTraceback = err.traceback ?? null;
    if (this.pythonTraceback) this.stack = `${this.stack ?? ''}\n\nPython traceback:\n${this.pythonTraceback}`;
  }
}

export type EventListener = (...args: unknown[]) => unknown;

const WS_URL = 'ws://127.0.0.1:1337/ws';
const RECONNECT_MS = 5000;
const log = logger('backend');

export class Backend {
  /** true while the socket is open. */
  readonly connected = new Store<boolean>(false);
  /** Counts successful opens: 1 after the first connect, 2+ after reconnects. */
  readonly opens = new Store<number>(0);
  private ws?: WebSocket;
  // Ids start at a random base per JS context: the backend's reply cache is keyed by id, so a
  // reloaded context counting from 1 again would get another context's cached replies.
  private nextId = Math.floor(Math.random() * 2 ** 40) + 1;
  private pending = new Map<number, PendingCall>();
  private listeners = new Map<string, Set<EventListener>>();
  private stopped = false;
  private firstOpen?: Promise<void>;

  /** Starts connecting; resolves on the first open, or after `waitMs` (calls queue meanwhile). */
  connect(waitMs = 10_000): Promise<void> {
    if (!this.firstOpen) {
      this.firstOpen = new Promise<void>((resolve) => {
        const off = this.connected.subscribe((c) => {
          if (c) {
            off();
            resolve();
          }
        });
        setTimeout(resolve, waitMs);
      });
      void this.open();
    }
    return this.firstOpen;
  }

  private async open(): Promise<void> {
    if (this.stopped) return;
    // a restarted backend has a new token; its bootstrap sets the globals again
    const token = currentToken();
    if (!token) {
      log.warn('no auth token yet');
      this.scheduleReconnect();
      return;
    }
    let ws: WebSocket;
    try {
      ws = new WebSocket(`${WS_URL}?auth=${encodeURIComponent(token)}`);
    } catch (e) {
      log.error('WebSocket constructor failed', e);
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      this.send({ type: MessageType.FULL_SYNC, messages: [...this.pending.values()].map((p) => p.msg) });
      this.connected.set(true);
      this.opens.set(this.opens.get() + 1);
      log.info(`connected (${this.pending.size} call(s) resent)`);
    };
    ws.onmessage = (ev) => this.onMessage(ev.data);
    ws.onclose = () => {
      if (this.ws !== ws) return;
      this.ws = undefined;
      this.connected.set(false);
      if (!this.stopped) {
        log.warn(`socket closed; reconnecting in ${RECONNECT_MS / 1000} s`);
        this.scheduleReconnect();
      }
    };
    ws.onerror = () => {
      // onclose follows and handles the retry
    };
  }

  private scheduleReconnect(): void {
    if (!this.stopped) setTimeout(() => void this.open(), RECONNECT_MS);
  }

  private send(msg: object): void {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(msg));
  }

  private onMessage(raw: unknown): void {
    let msg: { type?: number; id?: number; result?: unknown; error?: PyErrorPayload; event?: string; args?: unknown[] };
    try {
      msg = JSON.parse(String(raw));
    } catch {
      log.warn('non-JSON frame', raw);
      return;
    }
    switch (msg.type) {
      case MessageType.REPLY:
      case MessageType.ERROR:
      case MessageType.DISCARD: {
        const id = msg.id ?? -1;
        const p = this.pending.get(id);
        this.pending.delete(id);
        this.send({ type: MessageType.RECEIVED_RESPONSE, id });
        if (!p) return;
        if (msg.type === MessageType.REPLY) p.resolve(msg.result);
        else if (msg.type === MessageType.ERROR) p.reject(new PyError(msg.error ?? {}));
        // DISCARD: the plugin stopped mid-call; the promise is left unsettled on purpose.
        return;
      }
      case MessageType.EVENT:
        this.dispatch(msg.event ?? '', Array.isArray(msg.args) ? msg.args : []);
        return;
      default:
        log.warn('unknown message type', msg.type);
    }
  }

  private dispatch(event: string, args: unknown[]): void {
    const set = this.listeners.get(event);
    if (!set) return;
    for (const l of [...set]) {
      try {
        const r = l(...args);
        if (r && typeof (r as Promise<unknown>).catch === 'function') (r as Promise<unknown>).catch((e) => log.error(`async listener for ${event} failed`, e));
      } catch (e) {
        log.error(`listener for ${event} failed`, e);
      }
    }
  }

  call = <T = unknown>(route: string, ...args: unknown[]): Promise<T> => {
    const msg: CallMessage = { type: MessageType.CALL, id: this.nextId++, route, args };
    return new Promise<T>((resolve, reject) => {
      this.pending.set(msg.id, { msg, resolve: resolve as (v: unknown) => void, reject });
      this.send(msg);
    });
  };

  callable = <A extends unknown[] = unknown[], T = unknown>(route: string) =>
    (...args: A): Promise<T> => this.call<T>(route, ...args);

  addEventListener = (event: string, listener: EventListener): EventListener => {
    let set = this.listeners.get(event);
    if (!set) this.listeners.set(event, (set = new Set()));
    set.add(listener);
    return listener;
  };

  removeEventListener = (event: string, listener: EventListener): void => {
    this.listeners.get(event)?.delete(listener);
  };

  /** Stops reconnecting and closes the socket (used when a new Remora instance takes over). */
  deinit(): void {
    this.stopped = true;
    try {
      this.ws?.close();
    } catch {
      // closing
    }
    this.ws = undefined;
  }
}
