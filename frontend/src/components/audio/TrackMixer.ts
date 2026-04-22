/**
 * Track mixer — shows each of the six H6E tracks with its assignment, lets
 * Sam adjust per-speaker volume (saved into crop_config), mute / solo
 * individual tracks live through a Web Audio graph.
 *
 * Playback is an in-panel affordance separate from the SyncVerifier's
 * camera+H6E preview — here each of the six stems is loaded individually
 * so Sam can A/B tracks without crop setup getting in his way.
 */

import { h } from '../../lib/dom';
import { signal, effect } from '../../lib/signals';
import { createAudioGraph, type AudioGraph } from '../../lib/audio-graph';
import { Button } from '../Button';
import { Icon } from '../icons';
import { showToast } from '../../state/ui';
import { episodeDetail } from '../../state/episodes';

interface TrackRow {
  key: string;
  label: string;
  /** H6E stem convention (what appears in the WAV filename). */
  stem: 'Tr1' | 'Tr2' | 'Tr3' | 'Tr4' | 'TrLR' | 'TrMic';
  trackNumber: number | null;
}

const ROWS: TrackRow[] = [
  { key: 'tr1', label: 'Track 1', stem: 'Tr1', trackNumber: 1 },
  { key: 'tr2', label: 'Track 2', stem: 'Tr2', trackNumber: 2 },
  { key: 'tr3', label: 'Track 3', stem: 'Tr3', trackNumber: 3 },
  { key: 'tr4', label: 'Track 4', stem: 'Tr4', trackNumber: 4 },
  { key: 'mix', label: 'Stereo Mix', stem: 'TrLR', trackNumber: null },
  { key: 'mic', label: 'Built-in Mic', stem: 'TrMic', trackNumber: null },
];

const SPEAKER_VARS = [
  'var(--speaker-1)',
  'var(--speaker-2)',
  'var(--speaker-3)',
  'var(--speaker-4)',
];

interface MixerSpeaker {
  label: string;
  track: number | null;
  volume: number;
}
interface MixerAmbient {
  track_number?: number | null;
  stem?: string;
  volume: number;
}

interface TrackMixerProps {
  episodeId: string;
  /** Re-read per render so live assignment changes in the speaker panel
   *  flow through without rebuilding the audio graph. */
  getSpeakers: () => MixerSpeaker[];
  getAmbient: () => MixerAmbient[];
  onSpeakerVolume: (speakerIdx: number, volume: number) => void;
}

interface MixerState {
  graph: AudioGraph | null;
  loading: boolean;
  playing: boolean;
  error: string | null;
  solo: string | null;
  muted: Record<string, boolean>;
}

export function TrackMixer(props: TrackMixerProps): HTMLElement {
  const state = signal<MixerState>({
    graph: null,
    loading: false,
    playing: false,
    error: null,
    solo: null,
    muted: {},
  });

  const host = h('div', { class: 'panel p-5' });

  effect(() => {
    const s = state();
    host.replaceChildren(
      h(
        'div',
        { class: 'flex items-baseline justify-between mb-3' },
        h(
          'span',
          { class: 'text-heading-sm uppercase text-ink-tertiary' },
          'Track mixer'
        ),
        playButton(props.episodeId, state, s)
      ),
      h(
        'p',
        { class: 'text-body-sm text-ink-tertiary mb-3' },
        'Assignments follow the speaker panel. Use solo / mute to A/B tracks before saving.'
      ),
      h(
        'div',
        { class: 'flex flex-col divide-y divide-border-subtle' },
        ...ROWS.map((row) =>
          renderRow(row, props, state, s, props.getSpeakers(), props.getAmbient())
        )
      )
    );
  });

  // Clean up the audio graph when the mixer leaves the DOM.
  const cleanup = new MutationObserver(() => {
    if (!host.isConnected) {
      const g = state.peek().graph;
      if (g) g.dispose();
      cleanup.disconnect();
    }
  });
  cleanup.observe(document.body, { childList: true, subtree: true });

  return host;
}

function playButton(
  episodeId: string,
  state: ReturnType<typeof signal<MixerState>>,
  s: MixerState
): HTMLElement {
  const toggle = async (): Promise<void> => {
    const cur = state.peek();
    if (cur.graph && cur.playing) {
      cur.graph.pause();
      state.set({ ...cur, playing: false });
      return;
    }
    if (cur.graph) {
      cur.graph.play();
      state.set({ ...cur, playing: true });
      return;
    }
    // First play — lazy load every available stem. The backend's
    // audio-preview endpoint matches by filename stem, and H6E stems
    // carry the recorder's timestamp prefix (e.g. 260311_162356_Tr1).
    // We resolve each logical row to its actual stem via episode.audio_tracks.
    const stems = resolveStems();
    if (stems.length === 0) {
      state.set({
        ...cur,
        loading: false,
        error:
          'No H6E audio tracks on this episode — mixer preview needs the multi-track recorder.',
      });
      showToast('No H6E stems to preview.', 'error');
      return;
    }
    state.set({ ...cur, loading: true, error: null });
    const graph = createAudioGraph(
      stems.map((s) => ({
        key: s.key,
        url: `/api/episodes/${episodeId}/audio-preview/${encodeURIComponent(s.stem)}`,
      }))
    );
    try {
      await graph.ready;
      const errors: string[] = [];
      for (const [k, n] of graph.tracks.entries()) {
        if (n.error) errors.push(`${k}: ${n.error}`);
      }
      if (errors.length === ROWS.length) {
        state.set({
          ...state.peek(),
          loading: false,
          error: 'No stems loaded. Rendered audio may not exist yet.',
        });
        showToast('Audio preview failed — run audio_enhance first.', 'error');
        graph.dispose();
        return;
      }
      graph.play();
      state.set({ ...state.peek(), graph, loading: false, playing: true });
    } catch (e) {
      state.set({
        ...state.peek(),
        loading: false,
        error: (e as Error).message,
      });
    }
  };

  return Button({
    variant: s.playing ? 'primary' : 'secondary',
    size: 'sm',
    label: s.loading ? 'Loading…' : s.playing ? 'Stop' : 'Play all',
    icon: s.loading
      ? undefined
      : s.playing
      ? Icon.pause({ size: 14 })
      : Icon.play({ size: 14 }),
    loading: s.loading,
    onClick: toggle,
  });
}

/**
 * Map each logical TrackRow to the actual filename stem for this episode.
 * Returns only stems that have a matching filename on disk.
 */
function resolveStems(): Array<{ key: string; stem: string }> {
  const ep = episodeDetail.peek();
  const tracks =
    (ep?.audio_tracks as Array<Record<string, unknown>> | undefined) ?? [];
  if (tracks.length === 0) return [];
  const out: Array<{ key: string; stem: string }> = [];
  for (const row of ROWS) {
    // Match by filename ending (e.g. _Tr1.WAV, _TrLR.WAV, _TrMic.WAV)
    const needle = new RegExp(`_${row.stem}\\.`, 'i');
    const match = tracks.find((t) =>
      typeof t.filename === 'string' && needle.test(t.filename)
    );
    if (match) {
      const name = (match.filename as string).replace(/\.[^./]+$/, '');
      out.push({ key: row.key, stem: name });
    }
  }
  return out;
}

function renderRow(
  row: TrackRow,
  props: TrackMixerProps,
  state: ReturnType<typeof signal<MixerState>>,
  s: MixerState,
  speakers: MixerSpeaker[],
  ambientTracks: MixerAmbient[]
): HTMLElement {
  const speakerIdx = row.trackNumber
    ? speakers.findIndex((spk) => spk.track === row.trackNumber)
    : -1;
  const speaker = speakerIdx >= 0 ? speakers[speakerIdx] : null;
  const ambient = ambientTracks.find(
    (a) =>
      (a.track_number != null && a.track_number === row.trackNumber) ||
      (a.stem != null && a.stem === row.stem)
  );

  let assignment: HTMLElement;
  if (speaker && speakerIdx >= 0) {
    assignment = h(
      'span',
      { class: 'chip flex items-center gap-1.5' },
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
  const muted = !!s.muted[row.key];
  const soloed = s.solo === row.key;

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
      const g = state.peek().graph;
      if (g) g.setTrackGain(row.key, v);
    },
  });

  const muteBtn = h(
    'button',
    {
      onclick: () => {
        const next = !muted;
        const cur = state.peek();
        state.set({
          ...cur,
          muted: { ...cur.muted, [row.key]: next },
        });
        const g = cur.graph;
        if (g) g.setTrackMute(row.key, next);
      },
      class: [
        'w-7 h-7 rounded-md border text-code-sm font-mono tabular',
        muted
          ? 'bg-status-danger/20 border-status-danger/40 text-status-danger'
          : 'bg-surface-2 border-border text-ink-secondary hover:text-ink-primary',
      ].join(' '),
      title: muted ? 'Unmute' : 'Mute',
    },
    'M'
  );

  const soloBtn = h(
    'button',
    {
      onclick: () => {
        const next = soloed ? null : row.key;
        const cur = state.peek();
        state.set({ ...cur, solo: next });
        const g = cur.graph;
        if (g) g.setSolo(next);
      },
      class: [
        'w-7 h-7 rounded-md border text-code-sm font-mono tabular',
        soloed
          ? 'bg-accent text-ink-on-accent border-transparent'
          : 'bg-surface-2 border-border text-ink-secondary hover:text-ink-primary',
      ].join(' '),
      title: soloed ? 'Unsolo' : 'Solo',
    },
    'S'
  );

  return h(
    'div',
    {
      class:
        'grid grid-cols-[100px_60px_150px_1fr_56px] gap-3 items-center py-3',
    },
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
    h(
      'div',
      { class: 'flex gap-1' },
      muteBtn,
      soloBtn
    ),
    assignment,
    slider,
    h(
      'div',
      {
        class: [
          'text-code text-right font-mono tabular',
          muted
            ? 'text-status-danger'
            : speaker
            ? 'text-ink-primary'
            : 'text-ink-tertiary',
        ].join(' '),
      },
      muted ? 'MUTE' : `${Math.round(volume * 100)}%`
    )
  );
}
