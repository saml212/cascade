/** Canonical transcript speaker labels shared by review surfaces. */

export function transcriptSpeakerLabels(raw: unknown): Map<number, string> {
  const labels = new Map<number, string>();
  const add = (index: unknown, label: unknown): void => {
    const id = Number(index);
    const name = typeof label === 'string' ? label.trim() : '';
    if (Number.isInteger(id) && id >= 0 && name) labels.set(id, name);
  };
  if (Array.isArray(raw)) {
    for (const entry of raw) {
      if (entry && typeof entry === 'object') {
        const item = entry as Record<string, unknown>;
        add(item.index, item.label);
      }
    }
  } else if (raw && typeof raw === 'object') {
    for (const [index, label] of Object.entries(raw)) add(index, label);
  }
  return labels;
}

export function displaySpeakerLabel(
  speaker: unknown,
  labels: ReadonlyMap<number, string>
): string | null {
  const raw = typeof speaker === 'string' ? speaker.trim() : speaker;
  const match = typeof raw === 'string' ? /^speaker_(\d+)$/.exec(raw) : null;
  const index = typeof raw === 'number' ? raw : match ? Number(match[1]) : null;
  if (index != null && Number.isInteger(index) && index >= 0) {
    return labels.get(index) ?? `Speaker ${index + 1}`;
  }
  return typeof raw === 'string' && raw ? raw : null;
}
