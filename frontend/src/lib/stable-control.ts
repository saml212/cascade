export interface StableControl<T> {
  identity: string;
  value: T;
}

/** Reuse a stateful DOM control until the artifact it represents changes. */
export function stableControl<T>(
  current: StableControl<T> | undefined,
  identity: string,
  create: () => T
): StableControl<T> {
  return current?.identity === identity ? current : { identity, value: create() };
}

/** Keep a stateful control attached when a projection refresh returns its identity. */
export function mountStableControl(
  host: Element,
  next: Node | null,
  release?: (current: Node) => void,
): void {
  const current = host.firstChild;
  if (current === next) return;
  if (current) release?.(current);
  host.replaceChildren(...(next ? [next] : []));
}
