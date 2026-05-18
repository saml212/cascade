/**
 * Track mixer — shows every audio track found in episode.audio_tracks with
 * its assignment, lets Sam adjust per-speaker volume (saved into crop_config),
 * mute / solo individual tracks live through a Web Audio graph.
 *
 * Playback is an in-panel affordance separate from the SyncVerifier's
 * camera+H6E preview — here each stem is loaded individually so Sam can
 * A/B tracks without crop setup getting in his way.
 *
 * Rows are derived from the actual audio_tracks array rather than a
 * hardcoded list, so this works for:
 *   - H6E episodes (Tr1-Tr4, TrLR, TrMic, potentially multiple sessions)
 *   - Camera-stereo episodes (camera_Tr1, camera_Tr2)
 *   - Any future track type
 */

import { h } from '../../lib/dom';
import { signal, effect, type Signal } from '../../lib/signals';
import { createAudioGraph, type AudioGraph } from '../../lib/audio-graph';
import { Button } from '../Button';
import { Icon } from '../icons';
import { showToast } from '../../state/ui';
import { episodeDetail } from '../../state/episodes';

/** One row in the mixer, derived from a real audio_tracks entry. */
interface TrackRow {
  /** Stable key for DOM id / audio graph. Derived from filename without extension. */
  key: string;
  /** Human-readable label shown in the row header. */
  label: string;
  /** Stem name passed to /api/episodes/:id/audio-preview/:stem */
  stem: string;
  /** track_number from the audio_tracks entry (1-based for input tracks, null for mix/mic). */
  trackNumber: number | null;
  /** Session prefix when multiple H6E sessions exist, e.g. "260429_105232". Empty string = no disambig needed. */
  sessionLabel: string;
}

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

export interface TrackMixerProps {
  episodeId: string;
  /** Re-read per render so live assignment changes in the speaker panel
   *  flow through without rebuilding the audio graph. */
  getSpeakers: () => MixerSpeaker[];
  getAmbient: () => MixerAmbient[];
  onSpeakerVolume: (speakerIdx: number, volume: number) => void;
  /** Called when the assignment dropdown on a mixer row changes. Lets Sam
   *  reassign "Track N plays Host/Guest 1/etc." inline while auditioning
   *  tracks via solo — without leaving the mixer to hit the speaker panel.
   *  trackKey is the stem key of the row being assigned; speakerIdx is
   *  the target speaker (-1 = unassign). */
  onAssignTrack?: (trackNumber: number, speakerIdx: number) => void;
}

interface MixerState {
  graph: AudioGraph | null;
  loading: boolean;
  playing: boolean;
  error: string | null;
  solo: string | null;
  muted: Record<string, boolean>;
}

/**
 * Build TrackRow array from the current episode's audio_tracks.
 *
 * Groups tracks by the session prefix (the timestamp portion of filenames
 * like 260429_105232_Tr1.WAV). When multiple sessions exist the session
 * timestamp is prepended to the label for disambiguation.
 *
 * For camera-channel tracks (camera_Tr1.WAV / camera_Tr2.WAV) we emit a
 * "Camera L" / "Camera R" style label based on track_number.
 */
function buildRows(): TrackRow[] {
  const ep = episodeDetail.peek();
  const tracks =
    (ep?.audio_tracks as Array<Record<string, unknown>> | undefined) ?? [];
  if (tracks.length === 0) return [];

  // Detect whether multiple sessions exist (for H6E disambiguation).
  // Session prefix = everything before the last underscore-stem, e.g.
  // "260429_105232" from "260429_105232_Tr1.WAV".
  const sessionPrefixes = new Set<string>();
  for (const t of tracks) {
    const fn = (t.filename as string) ?? '';
    const m = fn.match(/^(\d{6}_\d{6})_/);
    if (m) sessionPrefixes.add(m[1]);
  }
  const multiSession = sessionPrefixes.size > 1;

  const rows: TrackRow[] = [];

  for (const t of tracks) {
    const fn = (t.filename as string) ?? '';
    if (!fn) continue;
    const stem = fn.replace(/\.[^./]+$/, ''); // strip extension
    const trackType = (t.track_type as string) ?? '';
    const trackNumber = (t.track_number as number | null | undefined) ?? null;

    // Session prefix, e.g. "260429_105232" — present only for H6E-style names.
    const sessionMatch = fn.match(/^(\d{6}_\d{6})_/);
    const sessionLabel = multiSession && sessionMatch ? sessionMatch[1] : '';

    // Human-readable label for the stem part (after the session prefix).
    let stemLabel: string;
    if (trackType === 'camera_channel') {
      // camera_Tr1.WAV → Camera L / Camera R
      if (trackNumber === 1) stemLabel = 'Camera L';
      else if (trackNumber === 2) stemLabel = 'Camera R';
      else stemLabel = `Camera Ch${trackNumber ?? '?'}`;
    } else if (trackType === 'input') {
      stemLabel = `Track ${trackNumber ?? '?'}`;
    } else if (trackType === 'stereo_mix') {
      stemLabel = 'Stereo Mix';
    } else if (trackType === 'builtin_mic') {
      stemLabel = 'Built-in Mic';
    } else {
      // Fallback: use the stem name itself
      stemLabel = stem.replace(/^(\d{6}_\d{6})_/, '');
    }

    rows.push({
      key: stem,
      label: stemLabel,
      stem,
      trackNumber,
      sessionLabel,
    });
  }

  return rows;
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

  // Snapshot rows once on mount — the set of tracks doesn't change during
  // a crop-setup session, so we don't need to rebuild the graph structure.
  // If the episode detail isn't loaded yet, we fall back to [] and the
  // effect below will re-run when it arrives.
  const rows = signal<TrackRow[]>(buildRows());

  const host = h('div', { class: 'panel p-5' });

  // Re-derive rows when episodeDetail first loads (it may arrive after mount).
  effect(() => {
    const ep = episodeDetail();
    if (ep) {
      const built = buildRows();
      if (built.length !== rows.peek().length) {
        rows.set(built);
      }
    }
  });

  effect(() => {
    const s = state();
    const currentRows = rows();
    host.replaceChildren(
      h(
        'div',
        { class: 'flex items-baseline justify-between mb-3' },
        h(
          'span',
          { class: 'text-heading-sm uppercase text-ink-tertiary' },
          'Track mixer'
        ),
        playButton(props.episodeId, state, s, currentRows)
      ),
      h(
        'p',
        { class: 'text-body-sm text-ink-tertiary mb-3' },
        'Hit Play, watch the level meters to see who is on which track, then assign in the dropdowns. Solo / mute to A/B before saving.'
      ),
      h(
        'div',
        { class: 'flex flex-col divide-y divide-border-subtle' },
        ...currentRows.map((row) =>
          renderRow(row, props, state, s, props.getSpeakers(), props.getAmbient())
        )
      )
    );
  });

  // Level-meter RAF loop. Reads analyser peak per track and writes the
  // bar width directly to each #mixer-meter-<key> element. Does NOT
  // touch the state signal — we don't want the whole mixer row tree
  // rebuilding every frame.
  let rafId: number | null = null;
  function levelLoop(): void {
    const g = state.peek().graph;
    if (g && state.peek().playing) {
      for (const row of rows.peek()) {
        const level = g.getTrackLevel(row.key);
        const el = document.getElementById(`mixer-meter-${row.key}`);
        if (el) el.style.width = `${Math.round(level * 100)}%`;
      }
    }
    rafId = window.requestAnimationFrame(levelLoop);
  }
  rafId = window.requestAnimationFrame(levelLoop);

  // Clean up the audio graph when the mixer leaves the DOM.
  const cleanup = new MutationObserver(() => {
    if (!host.isConnected) {
      const g = state.peek().graph;
      if (g) g.dispose();
      if (rafId != null) window.cancelAnimationFrame(rafId);
      rafId = null;
      cleanup.disconnect();
    }
  });
  cleanup.observe(document.body, { childList: true, subtree: true });

  return host;
}

function playButton(
  episodeId: string,
  state: Signal<MixerState>,
  s: MixerState,
  currentRows: TrackRow[]
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
    // First play — lazy load every available stem.
    if (currentRows.length === 0) {
      state.set({
        ...cur,
        loading: false,
        error:
          'No audio tracks on this episode — mixer preview is not available.',
      });
      showToast('No audio stems to preview.', 'error');
      return;
    }
    state.set({ ...cur, loading: true, error: null });
    const graph = createAudioGraph(
      currentRows.map((r) => ({
        key: r.key,
        url: `/api/episodes/${episodeId}/audio-preview/${encodeURIComponent(r.stem)}`,
      }))
    );
    try {
      await graph.ready;
      const errors: string[] = [];
      for (const [k, n] of graph.tracks.entries()) {
        if (n.error) errors.push(`${k}: ${n.error}`);
      }
      if (errors.length === currentRows.length) {
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

function renderRow(
  row: TrackRow,
  props: TrackMixerProps,
  state: Signal<MixerState>,
  s: MixerState,
  speakers: MixerSpeaker[],
  ambientTracks: MixerAmbient[]
): HTMLElement {
  // Match speaker by track_number. For multi-session H6E episodes the same
  // track_number (e.g. 1) appears in each session — we match the first speaker
  // that has this track assigned. The Track dropdown's value is the numeric
  // track_number so the existing speaker.track field still works.
  const speakerIdx = row.trackNumber != null
    ? speakers.findIndex((spk) => spk.track === row.trackNumber)
    : -1;
  const speaker = speakerIdx >= 0 ? speakers[speakerIdx] : null;
  const ambient = ambientTracks.find(
    (a) =>
      (a.track_number != null && a.track_number === row.trackNumber) ||
      (a.stem != null && a.stem === row.stem)
  );

  let assignment: HTMLElement;
  // Inline-editable speaker picker for numbered input tracks. Lets Sam
  // audition via solo, hear a voice, and reassign in one click without
  // leaving the mixer. Mix/mic tracks (trackNumber===null) stay as static chips.
  if (
    row.trackNumber != null &&
    props.onAssignTrack &&
    props.getSpeakers().length > 0
  ) {
    const spks = props.getSpeakers();
    assignment = h(
      'select',
      {
        class:
          'h-7 px-2 rounded-md border border-border bg-surface-2 text-ink-primary text-body-sm focus:outline-none focus:ring-2 focus:ring-accent',
        onchange: (e: Event) => {
          const v = (e.target as HTMLSelectElement).value;
          const idx = v === '' ? -1 : Number(v);
          props.onAssignTrack!(row.trackNumber!, idx);
        },
      },
      h('option', { value: '' }, '— Unassigned —'),
      ...spks.map((spk, i) =>
        h(
          'option',
          {
            value: String(i),
            selected: speakerIdx === i ? true : undefined,
          },
          spk.label
        )
      )
    );
  } else if (speaker && speakerIdx >= 0) {
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

  // Live level meter — filled by RAF loop in the mixer component. Pre-gain
  // tap (see audio-graph.ts), so the meter shows the underlying track
  // activity even when Sam mutes or solos.
  const meter = h(
    'div',
    {
      class:
        'relative h-2 rounded-full bg-surface-3 border border-border/60 overflow-hidden',
    },
    h('div', {
      id: `mixer-meter-${row.key}`,
      class:
        'h-full bg-gradient-to-r from-status-success via-accent to-status-danger transition-[width] duration-75',
      style: { width: '0%' },
    })
  );

  // Build the label cell. Show session label as a secondary line when
  // multiple sessions exist, so Sam can tell 260429_105232_Tr1 from
  // 260429_122543_Tr1.
  const labelCell = h(
    'div',
    null,
    h(
      'div',
      { class: 'text-body text-ink-primary font-medium' },
      row.label
    ),
    row.sessionLabel
      ? h(
          'div',
          { class: 'text-code-sm text-ink-tertiary font-mono tabular' },
          row.sessionLabel
        )
      : h(
          'div',
          { class: 'text-code-sm text-ink-tertiary font-mono tabular' },
          row.stem
        )
  );

  return h(
    'div',
    {
      class:
        'grid grid-cols-[100px_60px_150px_80px_1fr_56px] gap-3 items-center py-3',
    },
    labelCell,
    h(
      'div',
      { class: 'flex gap-1' },
      muteBtn,
      soloBtn
    ),
    assignment,
    meter,
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
