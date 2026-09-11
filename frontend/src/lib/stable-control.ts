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
