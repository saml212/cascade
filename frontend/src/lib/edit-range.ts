export interface SourceEdit {
  type: 'cut' | 'trim_start' | 'trim_end';
  start_seconds?: number;
  end_seconds?: number;
  seconds?: number;
}

/** Resolve every edit type to its removed range on the source clock. */
export function editSourceRange(
  edit: SourceEdit,
  sourceDuration: number
): [number, number] | null {
  if (!Number.isFinite(sourceDuration) || sourceDuration <= 0) return null;

  let start: number | undefined;
  let end: number | undefined;
  if (edit.type === 'trim_start') {
    start = 0;
    end = edit.seconds;
  } else if (edit.type === 'trim_end') {
    start = edit.seconds;
    end = sourceDuration;
  } else {
    start = edit.start_seconds;
    end = edit.end_seconds;
  }
  if (
    typeof start !== 'number' ||
    typeof end !== 'number' ||
    !Number.isFinite(start) ||
    !Number.isFinite(end)
  ) {
    return null;
  }

  const boundedStart = Math.max(0, Math.min(start, sourceDuration));
  const boundedEnd = Math.max(0, Math.min(end, sourceDuration));
  return boundedEnd >= boundedStart ? [boundedStart, boundedEnd] : null;
}
