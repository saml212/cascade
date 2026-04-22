/**
 * Minimal reactive primitive. Auto-tracks dependencies inside `effect` and
 * re-runs on signal change. Roughly 80 lines and no external deps.
 */

type Listener = { run: () => void; deps: Set<Set<Listener>> };

let currentListener: Listener | null = null;

function cleanup(l: Listener): void {
  for (const set of l.deps) set.delete(l);
  l.deps.clear();
}

export interface Signal<T> {
  (): T;
  set(next: T | ((prev: T) => T)): void;
  peek(): T;
}

export function signal<T>(initial: T): Signal<T> {
  let value = initial;
  const listeners = new Set<Listener>();

  const read = (() => {
    if (currentListener) {
      listeners.add(currentListener);
      currentListener.deps.add(listeners);
    }
    return value;
  }) as Signal<T>;

  read.set = (next) => {
    const resolved =
      typeof next === 'function' ? (next as (prev: T) => T)(value) : next;
    if (Object.is(resolved, value)) return;
    value = resolved;
    for (const l of [...listeners]) l.run();
  };

  read.peek = () => value;
  return read;
}

export function effect(fn: () => void): () => void {
  const l: Listener = {
    deps: new Set(),
    run: () => {
      cleanup(l);
      const prev = currentListener;
      currentListener = l;
      try {
        fn();
      } finally {
        currentListener = prev;
      }
    },
  };
  l.run();
  return () => cleanup(l);
}

