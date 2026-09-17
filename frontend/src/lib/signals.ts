/** Minimal synchronous signals, with explicit screen lifetime ownership. */
type Owner = { cleanups: Set<() => void>; disposed: boolean };
type Listener = { run: () => void; deps: Set<Set<Listener>>; disposed: boolean };
let currentListener: Listener | null = null;
let currentOwner: Owner | null = null;

export function onCleanup(dispose: () => void): void {
  currentOwner?.cleanups.add(dispose);
}

/** Own a screen's reactive work so navigation releases its subscriptions. */
export function effectScope(fn: () => void): () => void {
  const owner: Owner = { cleanups: new Set(), disposed: false };
  const previousOwner = currentOwner;
  const previousListener = currentListener;
  const dispose = () => {
    if (owner.disposed) return;
    owner.disposed = true;
    for (const cleanup of owner.cleanups) cleanup();
    owner.cleanups.clear();
  };
  currentOwner = owner;
  currentListener = null;
  try {
    fn();
  } catch (error) {
    dispose();
    throw error;
  } finally {
    currentOwner = previousOwner;
    currentListener = previousListener;
  }
  return dispose;
}

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
    const resolved = typeof next === 'function' ? (next as (prev: T) => T)(value) : next;
    if (Object.is(resolved, value)) return;
    value = resolved;
    for (const l of [...listeners]) l.run();
  };
  read.peek = () => value;
  return read;
}

export function effect(fn: () => void): () => void {
  const owner = currentOwner;
  const l: Listener = {
    deps: new Set(),
    disposed: false,
    run: () => {
      if (l.disposed || owner?.disposed) return;
      cleanup(l);
      const prev = currentListener;
      const previousOwner = currentOwner;
      currentListener = l;
      currentOwner = owner;
      try { fn(); } finally {
        currentListener = prev;
        currentOwner = previousOwner;
      }
    },
  };
  const dispose = () => { l.disposed = true; cleanup(l); };
  owner?.cleanups.add(dispose);
  l.run();
  return dispose;
}
