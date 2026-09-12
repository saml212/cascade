export const MIN_CROP_SPEAKERS = 2;
export const MAX_CROP_SPEAKERS = 4;

export interface CropSpeakerState {
  label: string;
  x: number;
  y: number;
  zoom: number;
  longform_x: number | null;
  longform_y: number | null;
  longform_zoom: number;
  track: number | null;
  volume: number;
}

export interface CropSpeakerBinding {
  asrSpeaker: number;
  label: string;
  reviewed: boolean;
}

export interface CropBindingState {
  byIndex: CropSpeakerBinding[][];
  unassigned: CropSpeakerBinding[];
}

export type SpeakerMapLoadStatus = 'loading' | 'ready' | 'unavailable';

function cropIndex(value: Record<string, unknown>): number | null {
  const explicit = value.crop_speaker_index;
  if (Number.isInteger(explicit) && (explicit as number) >= 0) return explicit as number;
  const match = /^speaker_(\d+)$/.exec(String(value.target_speaker ?? ''));
  return match ? Number(match[1]) : null;
}

/** Resolve the persisted array-index identities used by speaker-cut artifacts. */
export function cropBindingState(
  speakerMap: unknown,
  speakerCount: number
): CropBindingState {
  const byIndex = Array.from(
    { length: speakerCount },
    () => [] as CropSpeakerBinding[]
  );
  const unassigned: CropSpeakerBinding[] = [];
  if (!Array.isArray(speakerMap)) return { byIndex, unassigned };
  for (const value of speakerMap) {
    if (!value || typeof value !== 'object') continue;
    const mapping = value as Record<string, unknown>;
    const index = cropIndex(mapping);
    const asrSpeaker = mapping.index;
    if (!Number.isInteger(asrSpeaker) || (asrSpeaker as number) < 0) continue;
    const binding = {
      asrSpeaker: asrSpeaker as number,
      label: String(mapping.person ?? mapping.label ?? `ASR ${asrSpeaker}`),
      reviewed: mapping.mapping_method === 'manual_review',
    };
    if (index != null && index < speakerCount) byIndex[index].push(binding);
    else if (
      mapping.target_speaker === 'BOTH' ||
      ('crop_speaker_index' in mapping && mapping.crop_speaker_index === null)
    ) {
      unassigned.push(binding);
    }
  }
  return { byIndex, unassigned };
}

export function appendCropSpeaker(
  speakers: CropSpeakerState[],
  sourceWidth: number,
  sourceHeight: number
): CropSpeakerState[] {
  if (speakers.length >= MAX_CROP_SPEAKERS) return speakers;
  const nextIndex = speakers.length;
  const nextCount = nextIndex + 1;
  return [
    ...speakers,
    {
      label: `Speaker ${nextCount}`,
      x: sourceWidth > 0 ? Math.round((sourceWidth * nextCount) / (nextCount + 1)) : 0,
      y: sourceHeight > 0 ? Math.round(sourceHeight / 2) : 0,
      zoom: 1.4,
      longform_x: null,
      longform_y: null,
      longform_zoom: 0.75,
      track: null,
      volume: 1.0,
    },
  ];
}

export function removalBlockedReason(
  index: number,
  speakerCount: number,
  bindings: CropSpeakerBinding[][],
  bindingStatus: SpeakerMapLoadStatus
): string | null {
  if (speakerCount <= MIN_CROP_SPEAKERS) {
    return `At least ${MIN_CROP_SPEAKERS} speakers are required.`;
  }
  if (index !== speakerCount - 1) {
    return 'Only the final speaker can be removed because crop indexes are stable IDs.';
  }
  if (bindingStatus === 'loading') return 'Checking transcript identity…';
  if (bindingStatus === 'unavailable') {
    return 'Transcript identity could not be verified. Retry before removing this speaker.';
  }
  const reviewed = (bindings[index] ?? []).filter((binding) => binding.reviewed);
  if (reviewed.length) {
    const ids = reviewed.map((binding) => `ASR ${binding.asrSpeaker}`).join(', ');
    return `${ids} has a reviewed binding to this speaker. Update transcript identity first.`;
  }
  return null;
}
