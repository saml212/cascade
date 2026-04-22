/**
 * Track mixer — read-only view of how the 6 H6E tracks are assigned to
 * speakers or ambient. Volume sliders here nudge the speaker's track
 * volume (which is saved into the crop config on Save & continue).
 *
 * Tracks are fixed — Tr1, Tr2, Tr3, Tr4 plus the stereo Mix and built-in
 * Mic. The speaker panel is the source of truth for assignments, so this
 * component accepts a read-only snapshot plus a volume mutator.
 */

import { h } from '../../lib/dom';

interface TrackRow {
  key: string;
  label: string;
  stem: 'Tr1' | 'Tr2' | 'Tr3' | 'Tr4' | 'Mix' | 'Mic';
  trackNumber: number | null;
}

const ROWS: TrackRow[] = [
  { key: 'tr1', label: 'Track 1', stem: 'Tr1', trackNumber: 1 },
  { key: 'tr2', label: 'Track 2', stem: 'Tr2', trackNumber: 2 },
  { key: 'tr3', label: 'Track 3', stem: 'Tr3', trackNumber: 3 },
  { key: 'tr4', label: 'Track 4', stem: 'Tr4', trackNumber: 4 },
  { key: 'mix', label: 'Stereo Mix', stem: 'Mix', trackNumber: null },
  { key: 'mic', label: 'Built-in Mic', stem: 'Mic', trackNumber: null },
];

const SPEAKER_VARS = [
  'var(--speaker-1)',
  'var(--speaker-2)',
  'var(--speaker-3)',
  'var(--speaker-4)',
];

export interface TrackMixerProps {
  speakers: Array<{ label: string; track: number | null; volume: number }>;
  ambientTracks: Array<{ track_number?: number | null; stem?: string; volume: number }>;
  onSpeakerVolume: (speakerIdx: number, volume: number) => void;
}

export function TrackMixer(props: TrackMixerProps): HTMLElement {
  return h(
    'div',
    { class: 'panel p-5' },
    h(
      'div',
      { class: 'flex items-baseline justify-between mb-3' },
      h(
        'span',
        { class: 'text-heading-sm uppercase text-ink-tertiary' },
        'Track mixer'
      ),
      h(
        'span',
        { class: 'text-body-sm text-ink-tertiary' },
        'Assignments come from the speaker panel. Faders adjust that speaker’s track volume.'
      )
    ),
    h(
      'div',
      { class: 'flex flex-col divide-y divide-border-subtle' },
      ...ROWS.map((row) => renderRow(row, props))
    )
  );
}

function renderRow(row: TrackRow, props: TrackMixerProps): HTMLElement {
  const speakerIdx = row.trackNumber
    ? props.speakers.findIndex((s) => s.track === row.trackNumber)
    : -1;
  const speaker = speakerIdx >= 0 ? props.speakers[speakerIdx] : null;
  const ambient = props.ambientTracks.find(
    (a) =>
      (a.track_number != null && a.track_number === row.trackNumber) ||
      (a.stem != null && a.stem === row.stem)
  );

  let assignment: HTMLElement;
  if (speaker && speakerIdx >= 0) {
    assignment = h(
      'span',
      {
        class: 'chip flex items-center gap-1.5',
      },
      h('span', {
        class: 'w-1.5 h-1.5 rounded-full',
        style: { background: SPEAKER_VARS[speakerIdx % SPEAKER_VARS.length] },
      }),
      speaker.label
    );
  } else if (ambient) {
    assignment = h('span', { class: 'chip' }, 'Ambient');
  } else {
    assignment = h(
      'span',
      { class: 'chip text-ink-tertiary' },
      'Unassigned'
    );
  }

  const volume = speaker ? speaker.volume : ambient ? ambient.volume : 1.0;
  const volumeLabel = `${Math.round(volume * 100)}%`;
  const slider = h('input', {
    type: 'range',
    min: '0',
    max: '2',
    step: '0.05',
    value: String(volume),
    class: 'cascade-slider flex-1',
    disabled: !speaker,
    oninput: (e: Event) => {
      if (speakerIdx < 0) return;
      const v = Number((e.target as HTMLInputElement).value);
      props.onSpeakerVolume(speakerIdx, v);
    },
  });

  return h(
    'div',
    { class: 'grid grid-cols-[100px_170px_1fr_56px] gap-4 items-center py-3' },
    h(
      'div',
      null,
      h(
        'div',
        { class: 'text-body text-ink-primary font-medium' },
        row.label
      ),
      h(
        'div',
        { class: 'text-code-sm text-ink-tertiary font-mono tabular' },
        row.stem
      )
    ),
    assignment,
    slider,
    h(
      'div',
      {
        class: [
          'text-code text-right font-mono tabular',
          speaker ? 'text-ink-primary' : 'text-ink-tertiary',
        ].join(' '),
      },
      volumeLabel
    )
  );
}
