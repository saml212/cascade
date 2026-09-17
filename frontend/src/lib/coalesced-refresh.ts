/** Collapse overlapping refreshes into one active request plus one latest rerun. */
export function coalescedRefresh(run: () => Promise<void>): () => Promise<void> {
  let active: Promise<void> | null = null;
  let requested = false;

  return () => {
    requested = true;
    if (!active) {
      active = (async () => {
        let failure: { value: unknown } | null = null;
        while (requested) {
          requested = false;
          try {
            await run();
            failure = null;
          } catch (error) {
            failure = { value: error };
          }
        }
        if (failure) throw failure.value;
      })().finally(() => {
        active = null;
      });
    }
    return active;
  };
}
