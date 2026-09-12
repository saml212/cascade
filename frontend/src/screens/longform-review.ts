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
 * when utterances/edits/speakerLabels change. currentTime, inPoint, outPoint
 * updates are applied imperatively to existing row elements via rowRegistry.
 */

import { h, mount } from '../lib/dom';
import { signal, effect, type Signal } from '../lib/signals';
import { api, type EpisodeReviewState, type UnknownRecord } from '../lib/api';
import { describeStatus, episodeTitle, formatDuration, formatTimecode } from '../lib/format';
import { StatusPill } from '../components/StatusPill';
import { Button } from '../components/Button';
import { EpisodeBackButton } from '../components/EpisodeBackButton';
import { Icon } from '../components/icons';
import { navigate } from '../lib/router';
import { transcriptSpeakerLabels } from '../lib/speaker-labels';
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

function removedRange(edit: Edit, sourceDuration: number): [number, number] | null {
  if (sourceDuration <= 0) return null;
  const start =
    edit.type === 'trim_start'
      ? 0
      : edit.type === 'trim_end'
        ? edit.seconds ?? sourceDuration
        : edit.start_seconds ?? 0;
  const end =
    edit.type === 'trim_start'
      ? edit.seconds ?? 0
      : edit.type === 'trim_end'
        ? sourceDuration
        : edit.end_seconds ?? 0;
  const boundedStart = Math.max(0, Math.min(start, sourceDuration));
  const boundedEnd = Math.max(0, Math.min(end, sourceDuration));
  return boundedEnd > boundedStart ? [boundedStart, boundedEnd] : null;
}

function utteranceIsRemoved(
  utterance: Utterance,
  editList: Edit[],
  sourceDuration: number
): boolean {
  return editList.some((edit) => {
    const range = removedRange(edit, sourceDuration);
    return Boolean(
      range && utterance.start >= range[0] && utterance.end <= range[1]
    );
  });
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

const hhmmss = (seconds: number): string =>
  formatTimecode(Math.floor(seconds));

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
  const review = signal<EpisodeReviewState | null>(null);
  const edits = signal<Edit[]>([]);
  const utterances = signal<Utterance[]>([]);
  const speakerLabels = signal<Map<number, string>>(new Map());
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
      const [ep, es, state] = await Promise.all([
        api.getEpisode(episodeId),
        api.listEdits(episodeId),
        api.review(episodeId),
      ]);
      episode.set(ep);
      edits.set((es.edits as unknown) as Edit[]);
      review.set(state);
      loadError.set(null);
    } catch (e) {
      loadError.set((e as Error).message);
    }
  }

  async function loadTranscript(): Promise<void> {
    try {
      const data = await api.getTranscript(episodeId);
      utterances.set(data.utterances ?? []);

      speakerLabels.set(transcriptSpeakerLabels(data.speaker_map));
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

    const reviewState = review();
    if (!ep || !reviewState) {
      page.replaceChildren(loadingState());
      return;
    }

    const { header, body, footer } = buildPage(ep, reviewState);
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
      const inRemoved = utteranceIsRemoved(u, editList, duration.peek());
      const inPending =
        inn != null && out != null && inn < out && u.start >= inn && u.end <= out;
      applyRowState(row, { isPlaying, inCut: inRemoved, inPending });
    }

    lastHighlightedIdx = playingIdx;
  }

  /* ── Build the full page ─────────────────────────────────────────────── */

  function buildPage(ep: UnknownRecord, reviewState: EpisodeReviewState): {
    header: HTMLElement;
    body: HTMLElement;
    footer: HTMLElement;
  } {
    return {
      header: renderHeader(episodeId, ep),
      body: renderBody(ep, reviewState),
      footer: renderFooter(episodeId, ep, reviewState),
    };
  }

  function renderBody(ep: UnknownRecord, reviewState: EpisodeReviewState): HTMLElement {
    const artifact = reviewState.longform.render;

    const video = h('video', {
      src: reviewState.longform.source_preview_url,
      poster: `/api/episodes/${episodeId}/crop-frame`,
      controls: true,
      preload: 'metadata',
      class: 'w-full bg-black',
      style: { maxHeight: '54vh', display: 'block' },
      'aria-label': 'Source-clock editing video',
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

    const renderedReview =
      artifact.playable && artifact.url
        ? h(
            'details',
            { class: 'panel mt-4 overflow-hidden' },
            h(
              'summary',
              {
                class:
                  'cursor-pointer px-3 py-2 text-body-sm text-ink-secondary hover:text-ink-primary',
              },
              artifact.current
                ? 'Review current rendered output'
                : 'Review previous rendered output'
            ),
            h(
              'div',
              { class: 'border-t border-border-subtle' },
              h('video', {
                src: artifact.url,
                controls: true,
                preload: 'metadata',
                class: 'w-full bg-black block',
                style: { maxHeight: '42vh' },
                'aria-label': 'Edited-clock rendered video',
              }),
              h(
                'div',
                { class: 'px-3 py-2 text-body-sm text-ink-tertiary' },
                'Rendered output uses the edited clock. Set cuts only with the source player above.',
                !artifact.current
                  ? h('span', { class: 'block text-status-warning mt-1' }, artifact.detail)
                  : null,
                artifact.download_url
                  ? h(
                      'a',
                      {
                        href: artifact.download_url,
                        download: artifact.path.split('/').pop() ?? 'longform.mp4',
                        class:
                          'inline-block mt-2 text-ink-secondary hover:text-ink-primary underline underline-offset-4',
                      },
                      'Download rendered output'
                    )
                  : null
              )
            )
          )
        : null;

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
      h(
        'div',
        { class: 'panel overflow-hidden' },
        video,
        h(
          'div',
          {
            class:
              'px-3 py-2 border-t border-border-subtle text-body-sm text-ink-secondary',
          },
          'Source editing preview · source clock',
          h('span', { class: 'block text-ink-tertiary mt-0.5' },
            'Transcript rows, IN/OUT points, and saved cuts use this player’s timestamps.'
          )
        )
      ),
      timecodeEl,
      timelineHost,
      renderedReview
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

    /* ── Utterance list — rebuilds only when transcript inputs change ── */
    const listHost = h('div', { class: 'panel overflow-hidden' });
    effect(() => {
      const utts = utterances();
      const labelMap = new Map(speakerLabels());
      const editList = edits();
      const sourceDuration = duration();

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

      /* Transcript speaker IDs and crop indexes are separate namespaces. */
      for (const id of new Set(utts.map((utterance) => utterance.speaker))) {
        if (!labelMap.has(id) && cropSpeakers[id]?.label) {
          labelMap.set(id, cropSpeakers[id].label);
        }
      }

      /* Snapshot current time/in/out so initial states are correct */
      const ct = currentTime.peek();
      const inn = inPoint.peek();
      const out = outPoint.peek();

      const rows = utts.map((utt, i) =>
        buildUtteranceRow(
          utt,
          i,
          labelMap,
          editList,
          sourceDuration,
          ct,
          inn,
          out,
          videoRef,
          rowRegistry,
          inPoint,
          outPoint
        )
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

  function renderFooter(
    epId: string,
    ep: UnknownRecord,
    reviewState: EpisodeReviewState
  ): HTMLElement {
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
      const renderCurrent = reviewState.longform.render.current;
      const needsRender = !renderCurrent;
      const canApprove =
        renderCurrent &&
        !reviewState.longform.approval.current &&
        status.key === 'awaiting_longform_review';
      const alreadyPast =
        status.key === 'awaiting_clip_review' ||
        status.key === 'awaiting_publish' ||
        status.key === 'awaiting_backup' ||
        status.key === 'live';

      const headline = needsRender
        ? `${editList.length} cut${editList.length === 1 ? '' : 's'} saved · current render required`
        : canApprove
        ? 'Happy with this cut?'
        : alreadyPast
        ? 'Longform is already approved'
        : status.label;

      const sub = needsRender
        ? reviewState.longform.render.playable
          ? 'The previous file remains reviewable. Re-render before approving.'
          : 'Prepare the speaker-cut video before approving.'
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
          needsRender
            ? Button({
                variant: 'secondary',
                size: 'md',
                label: reviewState.longform.render.playable
                  ? 'Re-render current version'
                  : 'Prepare speaker-cut video',
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
          canApprove
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
    EpisodeBackButton(episodeId),
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
    const [start, end] = removedRange(e, dur) ?? [0, 0];
    const leftPct = (start / dur) * 100;
    const widthPct = Math.max(0.6, ((end - start) / dur) * 100);
    return h('button', {
      class:
        'absolute top-0 bottom-0 rounded hover:brightness-125 transition-[filter] duration-[120ms]',
      style: {
        left: `${leftPct}%`,
        width: `${widthPct}%`,
        backgroundColor:
          e.type === 'cut' ? 'rgba(226, 109, 90, 0.82)' : 'rgba(245, 165, 36, 0.78)',
      },
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
  sourceDuration: number,
  ct: number,
  inn: number | null,
  out: number | null,
  videoRef: { el: HTMLVideoElement | null },
  rowRegistry: Map<number, HTMLElement>,
  inPoint: Signal<number | null>,
  outPoint: Signal<number | null>
): HTMLElement {
  const isPlaying = ct >= utt.start && ct <= utt.end + 0.1;
  const inCut = utteranceIsRemoved(utt, editList, sourceDuration);
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
      Icon.chevronDown({ size: 14 })
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
