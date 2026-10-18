// Console logging with a Remora prefix. Only the console: Steam's DevTools (CEF :8080) is where
// people read it.
type Level = 'debug' | 'info' | 'warn' | 'error';

let verbose = false;
export function setVerbose(on: boolean): void {
  verbose = on;
}

export interface Logger {
  debug(...args: unknown[]): void;
  info(...args: unknown[]): void;
  warn(...args: unknown[]): void;
  error(...args: unknown[]): void;
}

export function logger(scope: string): Logger {
  const prefix = [`%c Remora %c ${scope} %c`, 'background:#1a9fff;color:#fff;border-radius:3px', 'color:#1a9fff', ''];
  const out = (level: Level) => (...args: unknown[]) => {
    if (level === 'debug' && !verbose) return;
    // eslint-disable-next-line no-console
    console[level === 'info' ? 'log' : level](...prefix, ...args);
  };
  return { debug: out('debug'), info: out('info'), warn: out('warn'), error: out('error') };
}
