/**
 * Longform Review — 3-pane scrub + cut UI.
 *
 * LEFT (40%): video player + cut-timeline lanes + timecode readout.
 * RIGHT (60%): pending-cut banner + utterance list with IN/OUT marking.
 * STICKY HEADER: back button, title, status pill.
 * STICKY FOOTER: cut summary + Apply / Approve buttons.
 *
 * Keyboard shortcuts (when no input focused):
 *   space  — toggle play/pause
 *   j / l  — seek -5s / +5s
 *   [      — set IN at utterance boundary around playhead
 *   ]      — set OUT at utterance boundary around playhead
 *   x      — commit IN/OUT as a cut
 *   u      — undo last cut
 *
 * Performance note: the utterance list (2559 rows for Arnold) is rebuilt only
 * when utterances/edits/speakerMap change. currentTime, inPoint, outPoint
 * updates are applied imperatively to existing row elements via rowRegistry.
 */

import { h, mount } from '../lib/dom';
import { signal, effect, type Signal } from '../lib/signals';
import { api, type UnknownRecord } from '../lib/api';
import { describeStatus, episodeTitle, formatDuration, formatTimecode } from '../lib/format';
import { StatusPill } from '../components/StatusPill';
import { Button } from '../components/Button';
import { link, navigate } from '../lib/router';
import { showToast } from '../state/ui';

/* ─── Types ─────────────────────────────────────────────────────────────── */

interface Edit {
  type: 'cut' | 'trim_start' | 'trim_end';
  start_seconds?: number;
  end_seconds?: number;
  seconds?: number;
  reason?: string;
}

interface Utterance {
  speaker: number;
  start: number;
  end: number;
  text: string;
}

interface SpeakerInfo {
  index: number;
  label: string;
  track?: number;
}

/* ─── Speaker colour ─────────────────────────────────────────────────────── */

const SPEAKER_COLORS = [
  'var(--speaker-1)',
  'var(--speaker-2)',
  'var(--speaker-3)',
  'var(--speaker-4)',
];

function speakerColor(speakerId: number): string {
  return SPEAKER_COLORS[speakerId % SPEAKER_COLORS.length];
}

/* ─── Timecode ───────────────────────────────────────────────────────────── */

function hhmmss(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const mm = String(m).padStart(2, '0');
  const ss = String(sec).padStart(2, '0');
  if (h > 0) return `${h}:${mm}:${ss}`;
  return `${mm}:${ss}`;
}

/* ─── Row state helper ───────────────────────────────────────────────────── */

interface RowState {
  isPlaying: boolean;
  inCut: boolean;
  inPending: boolean;
}

function applyRowState(row: HTMLElement, state: RowState): void {
  const { isPlaying, inCut, inPending } = state;

  let borderLeft = 'transparent';
  if (inCut) borderLeft = 'rgba(226,109,90,0.6)';
  else if (inPending) borderLeft = 'rgba(226,109,90,0.3)';
  else if (isPlaying) borderLeft = 'var(--accent)';

  let bg = 'transparent';
  if (isPlaying) bg = 'rgba(245,165,36,0.07)';
  else if (inPending) bg = 'rgba(226,109,90,0.04)';

  row.style.borderLeft = `3px solid ${borderLeft}`;
  row.style.background = bg;
  row.style.opacity = inCut ? '0.4' : '1';

  const textEl = row.querySelector<HTMLElement>('p[data-transcript-text]');
  if (textEl) {
    if (inCut) {
      textEl.classList.add('line-through', 'text-ink-tertiary');
      textEl.classList.remove('text-ink-primary');
    } else {
      textEl.classList.remove('line-through', 'text-ink-tertiary');
      textEl.classList.add('text-ink-primary');
    }
  }
}

/* ─── Main component ─────────────────────────────────────────────────────── */

export function LongformReview(target: HTMLElement, episodeId: string): void {
  /* Signals */
  const episode = signal<UnknownRecord | null>(null);
  const edits = signal<Edit[]>([]);
  const utterances = signal<Utterance[]>([]);
  const speakerMap = signal<SpeakerInfo[]>([]);
  /* Dict-form speaker_map from transcript: { "0": "Host", "1": "Guest 1", ... } */
  const speakerDictMap = signal<Record<string, string>>({});
  const chatSending = signal<boolean>(false);
  const loadError = signal<string | null>(null);

  const inPoint = signal<number | null>(null);
  const outPoint = signal<number | null>(null);
  const pendingReason = signal<string>('');

  const currentTime = signal<number>(0);
  const duration = signal<number>(0);

  /* Video ref */
  const videoRef: { el: HTMLVideoElement | null } = { el: null };

  /* Utterance row registry: index → row element (stable across imperative updates) */
  const rowRegistry: Map<number, HTMLElement> = new Map();

  /* Load */
  async function load(): Promise<void> {
    try {
      const [ep, es] = await Promise.all([
        api.getEpisode(episodeId),
        api.listEdits(episodeId),
      ]);
      episode.set(ep);
      edits.set((es.edits as unknown) as Edit[]);
      loadError.set(null);
    } catch (e) {
      loadError.set((e as Error).message);
    }
  }

  async function loadTranscript(): Promise<void> {
    try {
      const data = await api.getTranscript(episodeId);
      utterances.set(data.utterances ?? []);

      /* speaker_map may arrive as an array ({index,label,track}[]) from older
         speaker_cut runs, or as a string-keyed dict {"0":"Host",...} from newer
         runs. Handle both shapes. */
      const rawMap = (data as unknown as { speaker_map?: unknown }).speaker_map;
      if (Array.isArray(rawMap)) {
        speakerMap.set(rawMap as SpeakerInfo[]);
      } else if (rawMap && typeof rawMap === 'object') {
        speakerDictMap.set(rawMap as Record<string, string>);
      }
    } catch {
      /* Transcript not yet available — degrades gracefully */
    }
  }

  void load();
  void loadTranscript();

  /* ── Page shell ───────────────────────────────────────────────────────── */

  const page = h('div', { class: 'min-h-full flex flex-col' });

  effect(() => {
    const ep = episode();
    const err = loadError();

    if (err && !ep) {
      page.replaceChildren(
        h('div', { class: 'px-10 py-10 text-status-danger' }, err)
      );
      return;
    }

    if (!ep) {
      page.replaceChildren(loadingState());
      return;
    }

    const { header, body, footer } = buildPage(ep);
    page.replaceChildren(header, body, footer);
  });

  mount(target, page);

  /* ── Keyboard shortcuts ───────────────────────────────────────────────── */

  function isInputFocused(): boolean {
    const tag = (document.activeElement?.tagName ?? '').toLowerCase();
    return tag === 'input' || tag === 'textarea' || tag === 'select';
  }

  function utteranceAtTime(t: number): Utterance | null {
    const utts = utterances.peek();
    const exact = utts.find((u) => u.start <= t && t <= u.end + 0.1);
    if (exact) return exact;
    return utts.find((u) => u.start >= t) ?? null;
  }

  async function commitCut(): Promise<void> {
    const inn = inPoint.peek();
    const out = outPoint.peek();
    if (inn == null || out == null || inn >= out) {
      showToast('Set both IN and OUT points first.', 'error');
      return;
    }
    const reason = pendingReason.peek().trim();
    try {
      await api.addEdit(episodeId, {
        type: 'cut',
        start_seconds: inn,
        end_seconds: out,
        reason: reason || undefined,
      });
      inPoint.set(null);
      outPoint.set(null);
      pendingReason.set('');
      showToast('Cut marked.', 'success');
      await load();
    } catch (e) {
      showToast((e as Error).message, 'error');
    }
  }

  const keyHandler = (e: KeyboardEvent): void => {
    if (isInputFocused()) return;
    const v = videoRef.el;

    switch (e.key) {
      case ' ':
        e.preventDefault();
        if (v) {
          if (v.paused) void v.play();
          else v.pause();
        }
        break;
      case 'j':
        e.preventDefault();
        if (v) v.currentTime = Math.max(0, v.currentTime - 5);
        break;
      case 'l':
        e.preventDefault();
        if (v) v.currentTime = Math.min(v.duration || Infinity, v.currentTime + 5);
        break;
      case '[': {
        e.preventDefault();
        if (!v) return;
        const utt = utteranceAtTime(v.currentTime);
        inPoint.set(utt ? utt.start : v.currentTime);
        break;
      }
      case ']': {
        e.preventDefault();
        if (!v) return;
        const utt = utteranceAtTime(v.currentTime);
        outPoint.set(utt ? utt.end : v.currentTime);
        break;
      }
      case 'x':
        e.preventDefault();
        void commitCut();
        break;
      case 'u':
        e.preventDefault();
        void (async () => {
          const list = edits.peek();
          if (list.length === 0) return;
          try {
            await api.removeEdit(episodeId, list.length - 1);
            showToast('Last cut removed.');
            await load();
          } catch (err) {
            showToast((err as Error).message, 'error');
          }
        })();
        break;
    }
  };

  document.addEventListener('keydown', keyHandler);

  /* Cleanup keyboard listener when page leaves DOM */
  const observer = new MutationObserver(() => {
    if (!document.body.contains(page)) {
      document.removeEventListener('keydown', keyHandler);
      observer.disconnect();
    }
  });
  observer.observe(document.body, { childList: true, subtree: true });

  /* ── Auto-scroll throttle ─────────────────────────────────────────────── */
  let lastScrollAt = 0;
  let lastHighlightedIdx = -1;

  /* ── Imperative row-state updater (runs on timeupdate / in/out changes) ─ */

  function refreshRowHighlights(ct: number, inn: number | null, out: number | null): void {
    const utts = utterances.peek();
    const editList = edits.peek();

    /* Find currently-playing utterance */
    let playingIdx = -1;
    for (let i = 0; i < utts.length; i++) {
      const u = utts[i];
      if (ct >= u.start && ct <= u.end + 0.1) {
        playingIdx = i;
        break;
      }
    }

    /* Only iterate rows that need updating:
       previous playing row, new playing row, and pending range rows */
    const toUpdate = new Set<number>();

    if (lastHighlightedIdx >= 0) toUpdate.add(lastHighlightedIdx);
    if (playingIdx >= 0) toUpdate.add(playingIdx);

    /* Add rows in pending range — limit scan to relevant window */
    if (inn != null && out != null) {
      for (let i = 0; i < utts.length; i++) {
        const u = utts[i];
        if (u.end < inn) continue;
        if (u.start > out) break;
        toUpdate.add(i);
      }
    }

    for (const idx of toUpdate) {
      const row = rowRegistry.get(idx);
      if (!row) continue;
      const u = utts[idx];
      const isPlaying = ct >= u.start && ct <= u.end + 0.1;
      const inCut = editList.some(
        (e) =>
          e.type === 'cut' &&
          e.start_seconds != null &&
          e.end_seconds != null &&
          u.start >= e.start_seconds &&
          u.end <= e.end_seconds
      );
      const inPending =
        inn != null && out != null && inn < out && u.start >= inn && u.end <= out;
      applyRowState(row, { isPlaying, inCut, inPending });
    }

    lastHighlightedIdx = playingIdx;
  }

  /* ── Build the full page ─────────────────────────────────────────────── */

  function buildPage(ep: UnknownRecord): {
    header: HTMLElement;
    body: HTMLElement;
    footer: HTMLElement;
  } {
    return {
      header: renderHeader(episodeId, ep),
      body: renderBody(ep),
      footer: renderFooter(episodeId, ep),
    };
  }

  function renderBody(ep: UnknownRecord): HTMLElement {
    const status = describeStatus(ep.status as string);
    const hasLongform =
      status.key === 'awaiting_longform_review' ||
      status.key === 'awaiting_clip_review' ||
      status.key === 'awaiting_publish' ||
      status.key === 'awaiting_backup' ||
      status.key === 'live';

    if (!hasLongform) {
      return h(
        'div',
        { class: 'flex-1 px-8 py-8' },
        h(
          'div',
          { class: 'panel p-16 text-center' },
          h(
            'div',
            { class: 'font-display text-display-md text-ink-secondary mb-3' },
            'Longform render isn\'t ready.'
          ),
          h(
            'p',
            { class: 'text-body text-ink-tertiary max-w-md mx-auto' },
            'Cascade renders the longform after crop setup. You\'ll see the player here when it\'s done.'
          )
        )
      );
    }

    /* Video */
    const video = h('video', {
      src: `/media/episodes/${episodeId}/longform.mp4`,
      poster: `/api/episodes/${episodeId}/crop-frame`,
      controls: true,
      preload: 'metadata',
      class: 'w-full bg-black',
      style: { maxHeight: '54vh', display: 'block' },
    }) as HTMLVideoElement;
    videoRef.el = video;

    /* Timecode readout */
    const timecodeEl = h(
      'div',
      { class: 'text-code-sm font-mono tabular text-ink-secondary mt-2 px-1 select-none' },
      '00:00 / --:--'
    );

    video.addEventListener('loadedmetadata', () => {
      duration.set(video.duration);
      timecodeEl.textContent = `${hhmmss(0)} / ${hhmmss(video.duration)}`;
    });

    video.addEventListener('timeupdate', () => {
      const t = video.currentTime;
      currentTime.set(t);
      timecodeEl.textContent = `${hhmmss(t)} / ${hhmmss(video.duration || 0)}`;

      /* Imperative row highlight update */
      refreshRowHighlights(t, inPoint.peek(), outPoint.peek());

      /* Auto-scroll */
      const now = Date.now();
      if (now - lastScrollAt > 500) {
        const row = rowRegistry.get(lastHighlightedIdx);
        if (row) {
          lastScrollAt = now;
          row.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
      }
    });

    /* Timeline — reactive to edits + duration */
    const timelineHost = h('div', { class: 'mt-3' });
    effect(() => {
      const dur = duration();
      const editList = edits();
      if (dur > 0) {
        timelineHost.replaceChildren(renderTimeline(dur, editList, videoRef));
      }
    });

    /* ── Left pane ─────────────────────────────────────────────────────── */
    const leftPane = h(
      'div',
      {
        class: 'flex flex-col',
        style: {
          width: '40%',
          flexShrink: '0',
          position: 'sticky',
          top: '57px',
          maxHeight: 'calc(100vh - 57px - 68px)',
          overflowY: 'auto',
          padding: '20px 12px 20px 20px',
          alignSelf: 'flex-start',
        },
      },
      h('div', { class: 'panel overflow-hidden' }, video),
      timecodeEl,
      timelineHost
    );

    /* ── Pending-cut banner ─────────────────────────────────────────────── */
    const bannerHost = h('div', { class: 'mb-2' });
    effect(() => {
      const inn = inPoint();
      const out = outPoint();
      /* Also trigger row highlights when in/out change */
      refreshRowHighlights(currentTime.peek(), inn, out);
      if (inn == null && out == null) {
        bannerHost.replaceChildren();
        return;
      }
      bannerHost.replaceChildren(
        renderPendingBanner(inn, out, pendingReason, commitCut, () => {
          inPoint.set(null);
          outPoint.set(null);
          pendingReason.set('');
        })
      );
    });

    /* ── Utterance list — rebuilds only when utterances/edits/speakerMap change ── */
    const listHost = h('div', { class: 'panel overflow-hidden' });
    effect(() => {
      const utts = utterances();
      const sm = speakerMap();
      const dictMap = speakerDictMap();
      const editList = edits();

      rowRegistry.clear();

      if (utts.length === 0) {
        listHost.replaceChildren(
          h(
            'div',
            { class: 'p-10 text-center text-body text-ink-tertiary italic' },
            'Transcript not available — it will appear here once transcription is complete.'
          )
        );
        return;
      }

      /* Resolve crop_config speakers for fallback label lookup (index → label). */
      const cropSpeakers = (
        (ep.crop_config as { speakers?: Array<{ label: string }> } | undefined)
          ?.speakers ?? []
      );

      /* Build labelMap using three-priority order:
         1. dict-form speaker_map from transcript (newer agent output)
         2. crop_config.speakers[index].label (Sam's named speakers)
         3. array-form speaker_map from transcript (older agent output)
         4. final fallback: "Speaker N" (handled at call site) */
      const labelMap = new Map<number, string>();
      const allSpeakerIds = new Set<number>([
        ...sm.map((s) => s.index),
        ...utts.map((u) => u.speaker),
      ]);
      for (const id of allSpeakerIds) {
        const key = String(id);
        if (dictMap[key] !== undefined) {
          labelMap.set(id, dictMap[key]);
        } else if (cropSpeakers[id]?.label !== undefined) {
          labelMap.set(id, cropSpeakers[id].label);
        } else {
          const fromArray = sm.find((s) => s.index === id);
          if (fromArray) labelMap.set(id, fromArray.label);
        }
      }

      /* Snapshot current time/in/out so initial states are correct */
      const ct = currentTime.peek();
      const inn = inPoint.peek();
      const out = outPoint.peek();

      const rows = utts.map((utt, i) =>
        buildUtteranceRow(utt, i, labelMap, editList, ct, inn, out, videoRef, rowRegistry, inPoint, outPoint)
      );

      listHost.replaceChildren(...rows);
    });

    /* ── Right pane ─────────────────────────────────────────────────────── */
    const rightPane = h(
      'div',
      {
        style: {
          width: '60%',
          minWidth: '0',
          overflowY: 'auto',
          maxHeight: 'calc(100vh - 57px - 68px)',
          padding: '20px 20px 20px 8px',
          display: 'flex',
          flexDirection: 'column',
          gap: '0',
        },
      },
      bannerHost,
      listHost
    );

    /* ── Advanced (chat) panel ──────────────────────────────────────────── */
    const advancedPanel = renderAdvancedPanel(episodeId, chatSending, load);

    return h(
      'div',
      { class: 'flex-1 flex flex-col' },
      h(
        'div',
        {
          style: { display: 'flex', flex: '1', minHeight: '0' },
        },
        leftPane,
        rightPane
      ),
      h('div', { class: 'px-8 pb-6' }, advancedPanel)
    );
  }

  function renderFooter(epId: string, ep: UnknownRecord): HTMLElement {
    const footerEl = h(
      'footer',
      {
        class:
          'sticky bottom-0 z-20 border-t border-border-subtle bg-canvas/95 backdrop-blur-md px-8 py-4',
        style: { height: '68px' },
      }
    );

    effect(() => {
      const editList = edits();
      const status = describeStatus(ep.status as string);
      const pending = editList.length > 0;
      const canApprove = status.key === 'awaiting_longform_review';
      const alreadyPast =
        status.key === 'awaiting_clip_review' ||
        status.key === 'awaiting_publish' ||
        status.key === 'awaiting_backup' ||
        status.key === 'live';

      const totalRemoved = editList.reduce((sum, e) => {
        if (e.type === 'cut') return sum + ((e.end_seconds ?? 0) - (e.start_seconds ?? 0));
        if (e.type === 'trim_start' || e.type === 'trim_end') return sum + (e.seconds ?? 0);
        return sum;
      }, 0);

      const headline = pending
        ? `${editList.length} cut${editList.length === 1 ? '' : 's'} marked · ${formatDuration(totalRemoved)} removed`
        : canApprove
        ? 'Happy with this cut?'
        : alreadyPast
        ? 'Longform is already approved'
        : status.label;

      const sub = pending
        ? 'Apply edits to re-render before approving.'
        : canApprove
        ? 'Approving uploads to YouTube, updates the RSS feed, and fires clip mining.'
        : alreadyPast
        ? 'Downstream work has started. Request edits here to re-open.'
        : status.hint;

      footerEl.replaceChildren(
        h(
          'div',
          { class: 'max-w-[1600px] mx-auto flex items-center gap-4' },
          h(
            'div',
            { class: 'flex-1' },
            h('div', { class: 'text-body text-ink-primary font-medium' }, headline),
            h('div', { class: 'text-body-sm text-ink-secondary' }, sub)
          ),
          pending
            ? Button({
                variant: 'secondary',
                size: 'md',
                label: 'Apply edits & re-render',
                onClick: async () => {
                  try {
                    await api.applyEdits(epId);
                    showToast('Re-render queued.', 'success');
                    navigate(`/episodes/${epId}`);
                  } catch (e) {
                    showToast((e as Error).message, 'error');
                  }
                },
              })
            : null,
          !pending && canApprove
            ? Button({
                variant: 'primary',
                size: 'md',
                label: 'Approve longform',
                onClick: async () => {
                  try {
                    await api.approveLongform(epId);
                    showToast('Longform approved — clip mining begins.', 'success');
                    navigate(`/episodes/${epId}`);
                  } catch (e) {
                    showToast((e as Error).message, 'error');
                  }
                },
              })
            : null
        )
      );
    });

    return footerEl;
  }
}

/* ─── Loading state ─────────────────────────────────────────────────────── */

function loadingState(): HTMLElement {
  return h(
    'div',
    { class: 'px-10 py-10' },
    h('div', { class: 'panel h-96 animate-pulse-breath' })
  );
}

/* ─── Header ────────────────────────────────────────────────────────────── */

function renderHeader(episodeId: string, ep: UnknownRecord): HTMLElement {
  const status = describeStatus(ep.status as string);
  const title = episodeTitle(ep, episodeId);

  return h(
    'header',
    {
      class:
        'sticky top-0 z-10 bg-canvas border-b border-border-subtle px-8 py-3.5 flex items-center gap-5',
      style: { height: '57px' },
    },
    h(
      'a',
      {
        ...link(`/episodes/${episodeId}`),
        class:
          'w-8 h-8 flex items-center justify-center rounded-md text-ink-tertiary hover:text-ink-primary hover:bg-surface-2',
      },
      inlineSvgChevronLeft()
    ),
    h(
      'div',
      { class: 'flex-1 min-w-0' },
      h(
        'div',
        { class: 'flex items-center gap-3' },
        h(
          'span',
          { class: 'text-heading-sm uppercase tracking-wide text-ink-tertiary', 'data-review-label': '1' },
          'Longform review'
        ),
        StatusPill({ descriptor: status, size: 'sm' })
      ),
      h(
        'div',
        { class: 'text-body text-ink-primary font-medium mt-0.5 truncate' },
        title
      )
    )
  );
}

/* ─── Timeline ──────────────────────────────────────────────────────────── */

function renderTimeline(
  dur: number,
  editList: Edit[],
  videoRef: { el: HTMLVideoElement | null }
): HTMLElement {
  const seekTo = (seconds: number): void => {
    const v = videoRef.el;
    if (!v) return;
    v.currentTime = Math.max(0, Math.min(seconds, dur));
  };

  const lanes = editList.map((e, i) => {
    const start =
      e.type === 'trim_start'
        ? 0
        : e.type === 'trim_end'
        ? Math.max(0, dur - (e.seconds ?? 0))
        : e.start_seconds ?? 0;
    const end =
      e.type === 'trim_start'
        ? e.seconds ?? 0
        : e.type === 'trim_end'
        ? dur
        : e.end_seconds ?? 0;
    const leftPct = (start / dur) * 100;
    const widthPct = Math.max(0.6, ((end - start) / dur) * 100);
    const tone =
      e.type === 'cut'
        ? 'bg-status-danger/70'
        : 'bg-accent/60';
    return h('button', {
      class: `absolute top-0 bottom-0 rounded ${tone} hover:brightness-125 transition-[filter] duration-[120ms]`,
      style: { left: `${leftPct}%`, width: `${widthPct}%` },
      title: `${e.type} · ${formatTimecode(start)}–${formatTimecode(end)}${
        e.reason ? ` · ${e.reason}` : ''
      }\nClick to seek`,
      dataset: { idx: String(i) },
      onclick: (ev: MouseEvent) => {
        ev.stopPropagation();
        seekTo(start);
      },
    });
  });

  return h(
    'div',
    { class: 'panel p-4' },
    h(
      'div',
      { class: 'flex items-baseline justify-between mb-2' },
      h('span', { class: 'text-heading-sm uppercase text-ink-tertiary' }, 'Cut timeline'),
      h(
        'span',
        { class: 'text-body-sm text-ink-tertiary font-mono tabular' },
        `${formatDuration(dur)} · ${editList.length} edit${editList.length === 1 ? '' : 's'}`
      )
    ),
    h(
      'div',
      {
        class: 'relative h-8 rounded bg-surface-inset border border-border-subtle cursor-pointer',
        onclick: (ev: MouseEvent) => {
          const rect = (ev.currentTarget as HTMLElement).getBoundingClientRect();
          seekTo((ev.clientX - rect.left) / rect.width * dur);
        },
        title: 'Click to seek',
      },
      ...lanes
    ),
    h(
      'div',
      { class: 'flex justify-between text-code-sm text-ink-tertiary font-mono tabular mt-1' },
      h('span', null, '0:00'),
      h('span', null, formatTimecode(dur / 2)),
      h('span', null, formatTimecode(dur))
    )
  );
}

/* ─── Pending-cut banner ─────────────────────────────────────────────────── */

function renderPendingBanner(
  inn: number | null,
  out: number | null,
  pendingReason: Signal<string>,
  onCommit: () => Promise<void>,
  onCancel: () => void
): HTMLElement {
  const removed = inn != null && out != null && out > inn ? out - inn : null;
  const canCommit = inn != null && out != null && out > inn;

  const reasonInput = h('input', {
    type: 'text',
    class: [
      'flex-1 bg-surface-2 border border-border rounded px-3 py-1.5',
      'text-body-sm text-ink-primary placeholder:text-ink-disabled',
      'focus:border-accent focus:outline-none',
    ].join(' '),
    placeholder: 'Why? (optional)',
    value: pendingReason.peek(),
    oninput: (e: Event) => {
      pendingReason.set((e.target as HTMLInputElement).value);
    },
  }) as HTMLInputElement;

  return h(
    'div',
    {
      class: 'panel p-4 flex flex-col gap-3',
      style: {
        borderColor: 'rgba(226,109,90,0.35)',
        background: 'rgba(226,109,90,0.03)',
      },
    },
    h(
      'div',
      { class: 'flex items-center gap-3 flex-wrap' },
      h(
        'span',
        { class: 'text-status-danger font-mono tabular text-body-sm font-semibold' },
        inn != null ? `IN  ${hhmmss(inn)}` : 'IN  --:--'
      ),
      h('span', { class: 'text-ink-tertiary' }, '→'),
      h(
        'span',
        { class: 'text-status-danger font-mono tabular text-body-sm font-semibold' },
        out != null ? `OUT ${hhmmss(out)}` : 'OUT --:--'
      ),
      removed != null
        ? h(
            'span',
            { class: 'text-ink-tertiary text-body-sm' },
            `· ${formatDuration(removed)} removed`
          )
        : null
    ),
    h(
      'div',
      { class: 'flex items-center gap-2' },
      reasonInput,
      Button({
        variant: 'destructive',
        size: 'sm',
        label: 'Mark cut',
        disabled: !canCommit,
        onClick: () => void onCommit(),
      }),
      Button({
        variant: 'ghost',
        size: 'sm',
        label: 'Cancel',
        onClick: onCancel,
      })
    )
  );
}

/* ─── Utterance row builder ──────────────────────────────────────────────── */

function buildUtteranceRow(
  utt: Utterance,
  i: number,
  labelMap: Map<number, string>,
  editList: Edit[],
  ct: number,
  inn: number | null,
  out: number | null,
  videoRef: { el: HTMLVideoElement | null },
  rowRegistry: Map<number, HTMLElement>,
  inPoint: Signal<number | null>,
  outPoint: Signal<number | null>
): HTMLElement {
  const isPlaying = ct >= utt.start && ct <= utt.end + 0.1;
  const inCut = editList.some(
    (e) =>
      e.type === 'cut' &&
      e.start_seconds != null &&
      e.end_seconds != null &&
      utt.start >= e.start_seconds &&
      utt.end <= e.end_seconds
  );
  const inPending =
    inn != null && out != null && inn < out && utt.start >= inn && utt.end <= out;

  const label = labelMap.get(utt.speaker) ?? `Speaker ${utt.speaker}`;
  const color = speakerColor(utt.speaker);

  const textEl = h('p', {
    class: 'text-body-sm leading-relaxed',
    'data-transcript-text': '1',
  }, utt.text);

  const row = h(
    'div',
    {
      class: [
        'group flex items-start gap-3 px-4 py-2.5 cursor-pointer',
        'transition-colors duration-[80ms]',
        'hover:bg-surface-2',
      ].join(' '),
      style: {
        borderLeft: '3px solid transparent',
        background: 'transparent',
      },
      title: `${hhmmss(utt.start)} — click to seek`,
      onclick: (e: MouseEvent) => {
        if ((e.target as HTMLElement).closest('[data-mark-btn]')) return;
        const v = videoRef.el;
        if (v) v.currentTime = utt.start;
      },
    },
    /* Speaker chip */
    h(
      'span',
      {
        class: 'chip shrink-0 mt-0.5',
        style: {
          background: `${color}22`,
          color,
          borderColor: `${color}55`,
        },
      },
      label
    ),
    /* Content */
    h(
      'div',
      { class: 'flex-1 min-w-0 flex flex-col gap-0.5' },
      h(
        'span',
        { class: 'text-code-sm font-mono tabular text-ink-tertiary select-none' },
        hhmmss(utt.start)
      ),
      textEl
    ),
    /* IN/OUT buttons — show on hover */
    h(
      'div',
      {
        class: 'flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity duration-[100ms] shrink-0 mt-0.5',
      },
      h(
        'button',
        {
          class:
            'w-6 h-6 flex items-center justify-center rounded text-ink-tertiary hover:text-ink-primary hover:bg-surface-3 text-body-sm font-mono font-bold',
          title: 'Set IN point here ([ key)',
          'data-mark-btn': '1',
          onclick: (e: MouseEvent) => {
            e.stopPropagation();
            inPoint.set(utt.start);
          },
        },
        '['
      ),
      h(
        'button',
        {
          class:
            'w-6 h-6 flex items-center justify-center rounded text-ink-tertiary hover:text-ink-primary hover:bg-surface-3 text-body-sm font-mono font-bold',
          title: 'Set OUT point here (] key)',
          'data-mark-btn': '1',
          onclick: (e: MouseEvent) => {
            e.stopPropagation();
            outPoint.set(utt.end);
          },
        },
        ']'
      )
    )
  );

  /* Apply initial state */
  applyRowState(row, { isPlaying, inCut, inPending });

  rowRegistry.set(i, row);
  return row;
}

/* ─── Advanced (chat) panel ─────────────────────────────────────────────── */

function renderAdvancedPanel(
  episodeId: string,
  chatSending: Signal<boolean>,
  reload: () => Promise<void>
): HTMLElement {
  const input = h('textarea', {
    class: [
      'w-full bg-surface-2 border border-border rounded-lg px-4 py-3',
      'text-body text-ink-primary placeholder:text-ink-disabled',
      'resize-none focus:border-accent focus:outline-none leading-relaxed',
    ].join(' '),
    rows: '3',
    placeholder:
      'Trim the first 2 minutes. Cut the strip-club story around 42:00. Remove the coughing fit around 1:15:30.',
  }) as HTMLTextAreaElement;

  const submitHost = h('div');
  effect(() => {
    submitHost.replaceChildren(
      Button({
        variant: 'primary',
        size: 'md',
        label: chatSending() ? 'Working…' : 'Propose edits',
        loading: chatSending(),
        onClick: async () => {
          const msg = input.value.trim();
          if (!msg) return;
          chatSending.set(true);
          try {
            const res = await api.chat(
              episodeId,
              `Please propose longform edits based on this request: ${msg}`
            );
            input.value = '';
            if (res.actions_taken && res.actions_taken.length > 0) {
              showToast(`${res.actions_taken.length} edit(s) added.`, 'success');
            } else {
              showToast(res.response.slice(0, 200));
            }
            await reload();
          } catch (e) {
            showToast((e as Error).message, 'error');
          } finally {
            chatSending.set(false);
          }
        },
      })
    );
  });

  return h(
    'details',
    { class: 'panel mt-2' },
    h(
      'summary',
      {
        class: [
          'px-5 py-3 text-heading-sm uppercase tracking-wide text-ink-tertiary cursor-pointer',
          'select-none flex items-center justify-between',
          'hover:text-ink-primary transition-colors',
          '[&::-webkit-details-marker]:hidden',
        ].join(' '),
      },
      'Advanced: describe edits in plain text',
      inlineSvgChevronDown()
    ),
    h(
      'div',
      { class: 'px-5 pb-5 pt-3 flex flex-col gap-3 border-t border-border-subtle' },
      h(
        'p',
        { class: 'text-body-sm text-ink-secondary' },
        'Use plain language — Cascade parses it into cuts.'
      ),
      input,
      h('div', { class: 'flex items-center justify-end gap-2' }, submitHost)
    )
  );
}

/* ─── Inline SVG helpers ─────────────────────────────────────────────────── */

function makeSvg(d: string, w = 20, h = 20): SVGElement {
  const ns = 'http://www.w3.org/2000/svg';
  const el = document.createElementNS(ns, 'svg');
  el.setAttribute('viewBox', '0 0 20 20');
  el.setAttribute('fill', 'none');
  el.setAttribute('stroke', 'currentColor');
  el.setAttribute('stroke-width', '1.5');
  el.setAttribute('stroke-linecap', 'round');
  el.setAttribute('stroke-linejoin', 'round');
  el.setAttribute('width', String(w));
  el.setAttribute('height', String(h));
  el.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS(ns, 'path');
  path.setAttribute('d', d);
  el.appendChild(path);
  return el;
}

function inlineSvgChevronLeft(): SVGElement {
  return makeSvg('M12 4l-5 6 5 6');
}

function inlineSvgChevronDown(): SVGElement {
  return makeSvg('M4 7l6 5 6-5', 14, 14);
}
