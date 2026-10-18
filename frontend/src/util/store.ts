// A tiny observable value, used for state that both plain code and React components read.
export type Listener<T> = (value: T) => void;

export class Store<T> {
  private listeners = new Set<Listener<T>>();
  constructor(private value: T) {}

  get = (): T => this.value;

  set(value: T): void {
    if (Object.is(value, this.value)) return;
    this.value = value;
    for (const l of [...this.listeners]) {
      try {
        l(value);
      } catch (e) {
        console.error('[Remora] store listener failed', e);
      }
    }
  }

  update(fn: (v: T) => T): void {
    this.set(fn(this.value));
  }

  subscribe = (l: Listener<T>): (() => void) => {
    this.listeners.add(l);
    return () => this.listeners.delete(l);
  };
}
