/**
 * Clip Review — the editorial surface.
 *
 * Full-width page. A column of ClipCards (expand-on-click) plus a docked
 * chat input at the bottom that POSTs to /api/episodes/:id/chat. When the
 * agent executes actions that touch clip data, we reload the clip list so
 * the UI reflects the new state.
 */

import { h, mount } from '../lib/dom';
import { signal, effect, type Signal } from '../lib/signals';
import {
  api,
  type ClipReviewState,
  type EpisodeReviewState,
  type ReviewArtifact,
  type ReviewDestination,
  type UnknownRecord,
} from '../lib/api';
import {
  describeStatus,
  describeEpisodeStatus,
  episodeTitle,
  formatDuration,
  formatTimecode,
  pluralize,
  type StatusDescriptor,
} from '../lib/format';
import { StatusPill } from '../components/StatusPill';
import { Button } from '../components/Button';
import { Icon } from '../components/icons';
import { link, navigate } from '../lib/router';
import { showToast } from '../state/ui';

interface PlatformSpec {
  key: string;
  label: string;
  fields: Array<{ name: string; label: string; multiline?: boolean; hint?: string }>;
}

const PLATFORMS: PlatformSpec[] = [
  {
    key: 'youtube',
    label: 'YouTube Shorts',
    fields: [
      { name: 'title', label: 'Title', hint: 'Max 100 chars' },
      { name: 'description', label: 'Description', multiline: true },
      { name: 'hashtags', label: 'Hashtags', hint: 'Comma or space separated' },
    ],
  },
  {
    key: 'tiktok',
    label: 'TikTok',
    fields: [
      { name: 'caption', label: 'Caption', multiline: true, hint: 'Max 2200 chars' },
      { name: 'hashtags', label: 'Hashtags' },
    ],
  },
  {
    key: 'instagram',
    label: 'Instagram Reels',
    fields: [
      { name: 'caption', label: 'Caption', multiline: true },
      { name: 'hashtags', label: 'Hashtags' },
    ],
  },
  {
    key: 'x',
    label: 'X (Twitter)',
    fields: [{ name: 'text', label: 'Post text', multiline: true, hint: 'Max 280 chars' }],
  },
];

function enabledPlatforms(destinations: ReviewDestination[]): PlatformSpec[] {
  const enabled = new Set(destinations.map((destination) => destination.key));
  return PLATFORMS.filter((platform) => enabled.has(platform.key));
}

interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  actions?: UnknownRecord[];
}

export function ClipReview(
  target: HTMLElement,
  episodeId: string,
  initialClipId?: string
): void {
  const clips = signal<UnknownRecord[] | null>(null);
  const episode = signal<UnknownRecord | null>(null);
  const review = signal<EpisodeReviewState | null>(null);
  const expandedId = signal<string | null>(initialClipId ?? null);
  const loadError = signal<string | null>(null);
  const chatMessages = signal<ChatMessage[]>([]);
  const chatSending = signal<boolean>(false);

  async function load(): Promise<void> {
    try {
      const [ep, state] = await Promise.all([
        api.getEpisode(episodeId),
        api.review(episodeId),
      ]);
      episode.set(ep);
      review.set(state);
      clips.set(state.clips);
      loadError.set(null);
      if (state.clips.some((clip) => clip.review.render_job.status === 'rendering')) {
        window.setTimeout(() => void load(), 1500);
      }
    } catch (e) {
      loadError.set((e as Error).message);
    }
  }

  async function loadChatHistory(): Promise<void> {
    try {
      const history = (await api.chatHistory(episodeId)) as Array<UnknownRecord>;
      chatMessages.set(
        history.map((m) => ({
          role: (m.role as 'user' | 'assistant') ?? 'assistant',
          content: (m.content as string) ?? '',
          actions: m.actions_taken as UnknownRecord[] | undefined,
        }))
      );
    } catch {
      /* history endpoint may 404 on fresh episodes */
    }
  }

  async function sendChat(message: string): Promise<void> {
    if (!message.trim() || chatSending.peek()) return;
    chatMessages.set((prev) => [...prev, { role: 'user', content: message }]);
    chatSending.set(true);
    try {
      const res = await api.chat(episodeId, message);
      chatMessages.set((prev) => [
        ...prev,
        {
          role: 'assistant',
          content: res.response,
          actions: res.actions_taken,
        },
      ]);
      if (res.actions_taken && res.actions_taken.length > 0) await load();
    } catch (e) {
      chatMessages.set((prev) => [
        ...prev,
        {
          role: 'assistant',
          content: `I hit an error: ${(e as Error).message}`,
        },
      ]);
    } finally {
      chatSending.set(false);
    }
  }

  void load();
  void loadChatHistory();

  const body = h('div');

  effect(() => {
    const cs = clips();
    const err = loadError();

    if (err && !cs) {
      body.replaceChildren(errorCard(err));
      return;
    }
    if (!cs) {
      body.replaceChildren(
        h(
          'div',
          { class: 'panel p-10 animate-pulse-breath text-ink-tertiary' },
          'Loading clips…'
        )
      );
      return;
    }
    if (cs.length === 0) {
      body.replaceChildren(emptyClipsPanel());
      return;
    }

    const state = review();
    const platforms = enabledPlatforms(state?.enabled_destinations ?? []);

    body.replaceChildren(
      h(
        'div',
        { class: 'flex flex-col gap-4 pb-4' },
        ...cs.map((c) =>
          clipCard(
            episodeId,
            c,
            expandedId,
            (c.review as ClipReviewState),
            platforms,
            async () => load(),
            (nextId) => {
              expandedId.set(nextId);
              const suffix = nextId ? `/${encodeURIComponent(nextId)}` : '';
              window.history.replaceState(
                null,
                '',
                `#/episodes/${episodeId}/clips/review${suffix}`
              );
            }
          )
        )
      )
    );
  });

  mount(
    target,
    h(
      'div',
      { class: 'min-h-full flex flex-col' },
      renderHeader(episodeId, clips, episode, review),
      h(
        'div',
        { class: 'flex-1 min-h-0 overflow-y-auto' },
        h(
          'div',
          { class: 'max-w-[1080px] mx-auto px-4 sm:px-10 py-6 pb-32' },
          body
        )
      ),
      renderChatDock(chatMessages, chatSending, sendChat)
    )
  );
}

function renderHeader(
  episodeId: string,
  clipsSig: Signal<UnknownRecord[] | null>,
  episode: Signal<UnknownRecord | null>,
  review: Signal<EpisodeReviewState | null>
): HTMLElement {
  const title = h('div', {
    class: 'flex-1 min-w-0',
  });

  effect(() => {
    const ep = episode();
    const cs = clipsSig();
    const counts = review()?.clip_summary;
    const name = episodeTitle(ep ?? undefined, episodeId);
    const countLine = cs && counts
      ? `${pluralize(counts.selected_count, 'selected clip')} · ${counts.unselected_count} unselected · ${counts.rejected_count} rejected`
      : cs
        ? `${pluralize(cs.length, 'clip')} · ${episodeId}`
      : episodeId;
    title.replaceChildren(
      h(
        'div',
        { class: 'flex items-center gap-3' },
        h(
          'span',
          { class: 'text-heading-sm uppercase text-ink-tertiary' },
          'Clip review'
        ),
        ep
          ? StatusPill({
              descriptor: describeEpisodeStatus(ep, {
                cropConfig: ep.crop_config,
                clips: cs ?? undefined,
              }),
              size: 'sm',
            })
          : null
      ),
      h(
        'div',
        { class: 'text-body-lg text-ink-primary font-medium mt-1 truncate' },
        name
      ),
      h(
        'div',
        { class: 'text-body-sm text-ink-tertiary font-mono tabular mt-0.5' },
        countLine
      )
    );
  });

  const approveHost = h('div');
  effect(() => {
    const currentClips = clipsSig() ?? [];
    const state = review();
    const kept = currentClips.filter(
      (clip) =>
        (clip.review as ClipReviewState).selection.status === 'selected'
    );
    const byId = new Map(
      (state?.clips ?? []).map((clip) => [String(clip.id), clip])
    );
    const ready =
      kept.length > 0 &&
      kept.every((clip) =>
        Boolean(byId.get(String(clip.id ?? clip.clip_id))?.review.render.current)
      );
    approveHost.replaceChildren(
      Button({
        variant: 'primary',
        size: 'md',
        label: ready ? 'Final approve rendered clips' : 'Current renders required',
        disabled: !ready,
        title: ready
          ? 'Approve each current rendered file and its current copy'
          : 'Render or re-render every kept candidate first',
        onClick: async () => {
          try {
            await api.approveClips(
              episodeId,
              kept.map((clip) => String(clip.id ?? clip.clip_id))
            );
            showToast('Rendered clips approved for the current files.', 'success');
            navigate(`/episodes/${episodeId}`);
          } catch (e) {
            showToast((e as Error).message, 'error');
          }
        },
      })
    );
  });

  return h(
    'header',
    {
      class:
        'sticky top-0 z-10 bg-canvas border-b border-border-subtle px-8 py-4 flex items-center gap-5',
    },
    h(
      'a',
      {
        ...link(`/episodes/${episodeId}`),
        class:
          'w-8 h-8 flex items-center justify-center rounded-md text-ink-tertiary hover:text-ink-primary hover:bg-surface-2',
      },
      Icon.chevronLeft()
    ),
    title,
    Button({
      variant: 'secondary',
      size: 'md',
      label: 'Complete metadata',
      onClick: async () => {
        try {
          showToast('Auto-filling metadata…');
          await api.completeMetadata(episodeId);
          showToast('Metadata complete.', 'success');
          location.reload();
        } catch (e) {
          showToast((e as Error).message, 'error');
        }
      },
    }),
    approveHost
  );
}

function emptyClipsPanel(): HTMLElement {
  return h(
    'div',
    { class: 'panel p-16 text-center' },
    h(
      'div',
      { class: 'font-display text-display-md text-ink-secondary mb-3' },
      'No clips mined yet.'
    ),
    h(
      'p',
      { class: 'text-body text-ink-tertiary max-w-md mx-auto' },
      'The clip miner runs after longform approval. Come back once it’s finished — or ask the agent below to add a manual clip.'
    )
  );
}

function errorCard(err: string): HTMLElement {
  return h(
    'div',
    {
      class:
        'panel p-8 text-body text-status-danger border-status-danger/30',
    },
    err
  );
}

/* -------------------------------- Clip card ------------------------------- */

function clipCard(
  episodeId: string,
  clip: UnknownRecord,
  expandedId: Signal<string | null>,
  review: ClipReviewState,
  platforms: PlatformSpec[],
  reload: () => Promise<void>,
  setExpanded: (clipId: string | null) => void
): HTMLElement {
  const id = (clip.id as string) ?? (clip.clip_id as string);
  const title = (clip.title as string) || 'Untitled clip';
  const hook = (clip.hook_text as string) || (clip.hook as string) || '';
  const reason =
    (clip.compelling_reason as string) || (clip.reason as string) || '';
  const duration = (clip.duration as number) ?? 0;
  const start = (clip.start_seconds as number) ?? 0;
  const end = (clip.end_seconds as number) ?? 0;
  const score = (clip.virality_score as number) ?? null;
  const rank = (clip.rank as number) ?? null;
  const speaker = (clip.speaker as string) ?? '';
  const status = describeStatus((clip.status as string) ?? 'pending');
  const metadata = (clip.metadata as Record<string, UnknownRecord>) ?? {};

  const card = h('article', {
    id: `clip-${id}`,
    class:
      'panel scroll-mt-24 overflow-hidden transition-colors duration-[120ms]',
  });

  effect(() => {
    const expanded = expandedId() === id;
    card.classList.toggle('border-border-strong', expanded);

    const head = clipHead(
      id,
      title,
      hook,
      reason,
      duration,
      start,
      end,
      score,
      rank,
      speaker,
      status,
      expanded,
      review.render,
      review.selection.status,
      Boolean(clip.manual),
      () => setExpanded(expanded ? null : id)
    );
    const children: Node[] = [head];
    if (expanded) {
      children.push(
        clipExpanded(
          episodeId,
          id,
          start,
          end,
          metadata,
          review,
          platforms,
          reload
        )
      );
    }
    card.replaceChildren(...children);
    if (expanded) {
      requestAnimationFrame(() =>
        card.scrollIntoView({ behavior: 'smooth', block: 'start' })
      );
    }
  });

  return card;
}

function clipHead(
  id: string,
  title: string,
  hook: string,
  reason: string,
  duration: number,
  start: number,
  end: number,
  score: number | null,
  rank: number | null,
  speaker: string,
  status: StatusDescriptor,
  expanded: boolean,
  render: ReviewArtifact,
  selection: ClipReviewState['selection']['status'],
  manual: boolean,
  toggle: () => void
): HTMLElement {
  return h(
    'button',
    {
      type: 'button',
      'aria-expanded': expanded,
      'aria-controls': `clip-review-${id}`,
      'aria-label': `${expanded ? 'Collapse' : 'Review'} ${title}`,
      class:
        'w-full p-4 sm:p-5 grid grid-cols-[88px_1fr_auto] sm:grid-cols-[140px_1fr_auto] gap-3 sm:gap-5 items-start text-left hover:bg-surface-2/40',
      onclick: toggle,
    },
    clipThumb(duration, render),
    h(
      'div',
      { class: 'min-w-0' },
      h(
        'div',
        { class: 'flex items-center gap-2 mb-2 flex-wrap' },
        rank != null
          ? h('span', { class: 'chip font-mono tabular' }, `#${rank}`)
          : null,
        score != null && !(manual && score === 0)
          ? h('span', { class: 'chip font-mono tabular' }, `Candidate score ${score}/10`)
          : manual
            ? h('span', { class: 'chip' }, 'Manual selection · unscored')
          : null,
        h(
          'span',
          { class: 'chip font-mono tabular' },
          `${formatTimecode(start)}–${formatTimecode(end)}`
        ),
        h(
          'span',
          { class: 'chip font-mono tabular' },
          formatDuration(duration)
        ),
        speaker ? h('span', { class: 'chip' }, speaker) : null,
        h(
          'span',
          { class: 'chip' },
          selection === 'selected'
            ? 'Selected'
            : selection === 'rejected'
              ? 'Rejected'
              : 'Unselected'
        ),
        StatusPill({ descriptor: status, size: 'sm' }),
        h(
          'span',
          {
            class: `chip ${
              render.current
                ? 'text-status-success'
                : render.playable
                  ? 'text-status-warning'
                  : 'text-ink-tertiary'
            }`,
          },
          render.current
            ? 'Current render'
            : render.playable
              ? 'Render out of date'
              : 'Not rendered'
        )
      ),
      h(
        'h3',
        { class: 'text-heading-lg text-ink-primary' },
        title
      ),
      hook
        ? h(
            'p',
            {
              class:
                'font-display text-body-lg text-ink-secondary mt-2 leading-relaxed',
            },
            hook
          )
        : null,
      reason
        ? h(
            'p',
            { class: 'text-body text-ink-tertiary mt-2 leading-relaxed' },
            reason
          )
        : null
    ),
    h(
      'span',
      {
        class: `text-ink-tertiary transition-transform duration-[200ms] mt-2 ${
          expanded ? 'rotate-180' : ''
        }`,
          'aria-hidden': 'true',
      },
      Icon.chevronDown()
    )
  );
}

function clipThumb(
  duration: number,
  render: ReviewArtifact
): HTMLElement {
  // Only set a src when the shorts MP4 actually exists on disk.
  // Without this guard every card fires a 404 for the missing file.
  let innerEl: HTMLElement;
  let hoverHandlers: Record<string, unknown> = {};

  if (render.playable && render.url) {
    const url = render.url;
    const video = h('video', {
      src: url,
      muted: true,
      playsinline: true,
      preload: 'metadata',
      tabindex: '-1',
      'aria-hidden': 'true',
      class: 'w-full h-full object-cover bg-surface-inset pointer-events-none',
    }) as HTMLVideoElement;
    innerEl = video;
    hoverHandlers = {
      onmouseenter: () => video.play().catch(() => {}),
      onmouseleave: () => {
        video.pause();
        video.currentTime = 0;
      },
    };
  } else {
    // Placeholder — no network request, no 404
    innerEl = h(
      'div',
      {
        class:
          'w-full h-full bg-surface-inset flex items-center justify-center text-body-sm text-ink-tertiary',
      },
      'Not rendered'
    );
  }

  return h(
    'div',
    {
      class:
        'w-[88px] sm:w-[140px] aspect-[9/16] rounded-md overflow-hidden bg-surface-inset relative',
      ...hoverHandlers,
    },
    innerEl,
    h(
      'div',
      {
        class:
          'absolute bottom-1 right-1 text-code-sm text-ink-primary font-mono tabular bg-black/60 rounded px-1.5 py-0.5',
      },
      formatDuration(duration)
    )
  );
}

/* -------------------- Expanded clip actions + metadata ------------------- */

function clipExpanded(
  episodeId: string,
  clipId: string,
  start: number,
  end: number,
  metadata: Record<string, UnknownRecord>,
  review: ClipReviewState,
  platforms: PlatformSpec[],
  reload: () => Promise<void>
): HTMLElement {
  return h(
    'div',
    { id: `clip-review-${clipId}`, class: 'border-t border-border-subtle' },
    renderReviewPlayer(clipId, review.render),
    renderActions(episodeId, clipId, review, reload),
    renderTrim(episodeId, clipId, start, end, reload),
    renderMetadataAccordion(
      episodeId,
      clipId,
      metadata,
      platforms,
      review.metadata,
      reload
    )
  );
}

function renderReviewPlayer(
  clipId: string,
  render: ReviewArtifact
): HTMLElement {
  if (!render.playable || !render.url) {
    return h(
      'section',
      {
        class:
          'px-5 py-8 bg-surface-inset/50 text-center border-b border-border-subtle',
        'aria-label': 'Clip video review',
      },
      h('p', { class: 'text-body text-ink-secondary' }, 'No current video to review.'),
      h(
        'p',
        { class: 'text-body-sm text-ink-tertiary mt-1' },
        'Render this candidate to inspect framing, captions, audio, and timing.'
      )
    );
  }

  const url = render.url;
  return h(
    'section',
    {
      class:
        'px-5 py-5 bg-surface-inset/50 border-b border-border-subtle',
      'aria-label': 'Clip video review',
    },
    !render.current
      ? h(
          'div',
          {
            class:
              'max-w-[720px] mx-auto mb-4 rounded-md border border-status-warning/40 bg-status-warning/10 px-4 py-3',
            role: 'status',
          },
          h(
            'div',
            { class: 'text-body font-medium text-status-warning' },
            'Out of date · re-render before final approval'
          ),
          h('div', { class: 'text-body-sm text-ink-secondary mt-1' }, render.detail)
        )
      : null,
    h(
      'div',
      { class: 'w-full max-w-[390px] mx-auto' },
      h('video', {
        src: url,
        controls: true,
        playsinline: true,
        preload: 'metadata',
        class:
          'block w-full max-h-[68vh] aspect-[9/16] object-contain bg-black rounded-lg border border-border-strong shadow-lift-lg',
        'aria-label': `Review video for ${clipId}`,
      }),
      h(
        'div',
        { class: 'flex items-center justify-between gap-3 mt-3' },
        h(
          'span',
          { class: 'text-body-sm text-ink-tertiary' },
          render.current ? 'Current rendered file' : 'Previous rendered file'
        ),
        h(
          'a',
          {
            href: url,
            download: `${clipId}.mp4`,
            class:
              'text-body-sm text-ink-secondary hover:text-ink-primary underline underline-offset-4',
          },
          'Download video'
        )
      )
    )
  );
}

function renderActions(
  episodeId: string,
  clipId: string,
  review: ClipReviewState,
  reload: () => Promise<void>
): HTMLElement {
  const rendering = signal(review.render_job.status === 'rendering');
  const primary = h('span');
  effect(() => {
    const active = rendering();
    const approved = review.approval.current;
    primary.replaceChildren(
      Button({
        variant: 'primary',
        size: 'sm',
        label: approved
          ? 'Approved'
          : review.render.current
            ? 'Final approve'
            : active
              ? 'Rendering…'
              : review.render.playable
                ? 'Re-render clip'
                : 'Render clip',
        disabled: approved || active,
        loading: active,
        onClick: async () => {
          try {
            if (review.render.current) {
              await api.approveClip(episodeId, clipId);
              showToast('Current render approved.', 'success');
            } else {
              rendering.set(true);
              await api.selectClip(episodeId, clipId);
              showToast('Rendering the selected clip locally…');
              await api.renderClip(episodeId, clipId);
              showToast('Clip rendered. Review it before final approval.', 'success');
            }
            await reload();
          } catch (e) {
            showToast((e as Error).message, 'error');
          } finally {
            rendering.set(false);
          }
        },
      })
    );
  });

  return h(
    'div',
    { class: 'flex items-center gap-2 px-5 py-4 flex-wrap' },
    primary,
    Button({
      variant: 'destructive',
      size: 'sm',
      label: 'Reject',
      onClick: async () => {
        try {
          await api.rejectClip(episodeId, clipId);
          showToast('Rejected.');
          await reload();
        } catch (e) {
          showToast((e as Error).message, 'error');
        }
      },
    }),
    Button({
      variant: 'ghost',
      size: 'sm',
      label: 'Alternative',
      title: 'Ask the clip miner for a similar clip',
      onClick: async () => {
        try {
          await api.alternativeClip(episodeId, clipId);
          showToast('Alternative requested.');
          await reload();
        } catch (e) {
          showToast((e as Error).message, 'error');
        }
      },
    })
  );
}

function renderTrim(
  episodeId: string,
  clipId: string,
  initialStart: number,
  initialEnd: number,
  reload: () => Promise<void>
): HTMLElement {
  const startStr = signal<string>(formatTimecode(initialStart));
  const endStr = signal<string>(formatTimecode(initialEnd));

  const startInput = h('input', {
    type: 'text',
    value: startStr(),
    class: trimInputClass,
    oninput: (e: Event) =>
      startStr.set((e.target as HTMLInputElement).value),
  }) as HTMLInputElement;
  const endInput = h('input', {
    type: 'text',
    value: endStr(),
    class: trimInputClass,
    oninput: (e: Event) =>
      endStr.set((e.target as HTMLInputElement).value),
  }) as HTMLInputElement;

  return h(
    'div',
    {
      class:
        'flex items-end gap-4 px-5 py-4 border-t border-border-subtle flex-wrap',
    },
    h(
      'div',
      null,
      h(
        'label',
        { class: 'block text-heading-sm uppercase text-ink-tertiary mb-1' },
        'Start'
      ),
      startInput
    ),
    h(
      'div',
      null,
      h(
        'label',
        { class: 'block text-heading-sm uppercase text-ink-tertiary mb-1' },
        'End'
      ),
      endInput
    ),
    Button({
      variant: 'secondary',
      size: 'md',
      label: 'Save trim',
      onClick: async () => {
        const s = parseTimecode(startStr.peek());
        const e = parseTimecode(endStr.peek());
        if (s == null || e == null || e <= s) {
          showToast('Give me valid start/end times (mm:ss).', 'error');
          return;
        }
        try {
          await api.updateClip(episodeId, clipId, {
            start_seconds: s,
            end_seconds: e,
          });
          showToast('Trim saved.', 'success');
          await reload();
        } catch (err) {
          showToast((err as Error).message, 'error');
        }
      },
    })
  );
}

const trimInputClass =
  'w-28 h-9 bg-surface-2 border border-border rounded-md px-2.5 text-body text-ink-primary font-mono tabular focus:border-accent focus:outline-none';

function parseTimecode(s: string): number | null {
  const parts = s.trim().split(':').map((p) => Number(p));
  if (parts.some((p) => Number.isNaN(p))) return null;
  if (parts.length === 1) return parts[0];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  return null;
}

/* ------------------------- Per-platform metadata ------------------------- */

function renderMetadataAccordion(
  episodeId: string,
  clipId: string,
  metadata: Record<string, UnknownRecord>,
  platforms: PlatformSpec[],
  metadataState: ClipReviewState['metadata'],
  reload: () => Promise<void>
): HTMLElement {
  const openPlatform = signal<string | null>(null);

  const host = h('div', {
    class: 'border-t border-border-subtle',
  });

  effect(() => {
    const current = openPlatform();
    host.replaceChildren(
      h(
        'div',
        {
          class: 'px-5 py-3 flex items-center justify-between',
        },
        h(
          'span',
          { class: 'text-heading-sm uppercase text-ink-tertiary' },
          'Per-platform metadata'
        ),
        h(
          'span',
          { class: 'text-body-sm text-ink-tertiary' },
          `${metadataState.complete_destination_count} of ${metadataState.enabled_destination_count} ready`
        )
      ),
      ...platforms.map((spec) =>
        platformRow(
          episodeId,
          clipId,
          spec,
          metadata[spec.key] ?? {},
          current === spec.key,
          () =>
            openPlatform.set((prev) => (prev === spec.key ? null : spec.key)),
          reload
        )
      )
    );
  });

  return host;
}

function platformRow(
  episodeId: string,
  clipId: string,
  spec: PlatformSpec,
  current: UnknownRecord,
  open: boolean,
  toggle: () => void,
  reload: () => Promise<void>
): HTMLElement {
  const firstValue = spec.fields
    .map((f) => current[f.name])
    .find((v) => typeof v === 'string' && v.length > 0) as string | undefined;
  const hasAny = firstValue != null;

  return h(
    'div',
    { class: 'border-t border-border-subtle' },
    h(
      'button',
      {
        onclick: toggle,
        class:
          'w-full text-left px-5 py-3 flex items-center gap-3 hover:bg-surface-2/40 transition-colors duration-[120ms]',
      },
      h(
        'span',
        { class: 'text-body text-ink-primary font-medium w-40 shrink-0' },
        spec.label
      ),
      h(
        'span',
        {
          class: [
            'flex-1 truncate text-body-sm',
            hasAny ? 'text-ink-secondary' : 'text-ink-tertiary italic',
          ].join(' '),
        },
        hasAny ? firstValue! : 'Empty'
      ),
      h(
        'span',
        {
          class: `text-ink-tertiary transition-transform duration-[200ms] ${
            open ? 'rotate-180' : ''
          }`,
        },
        Icon.chevronDown({ size: 16 })
      )
    ),
    open
      ? platformEditor(episodeId, clipId, spec, current, reload)
      : null
  );
}

function platformEditor(
  episodeId: string,
  clipId: string,
  spec: PlatformSpec,
  current: UnknownRecord,
  reload: () => Promise<void>
): HTMLElement {
  const draft: Record<string, string> = {};
  for (const f of spec.fields) {
    const raw = current[f.name];
    if (Array.isArray(raw)) draft[f.name] = (raw as string[]).join(' ');
    else if (typeof raw === 'string') draft[f.name] = raw;
    else draft[f.name] = '';
  }

  const inputs = spec.fields.map((f) => {
    const el = f.multiline
      ? (h('textarea', {
          class: [
            'w-full bg-surface-2 border border-border rounded-md px-3 py-2 text-body text-ink-primary',
            'focus:border-accent focus:outline-none leading-relaxed',
          ].join(' '),
          rows: '4',
          value: draft[f.name],
          oninput: (e: Event) =>
            (draft[f.name] = (e.target as HTMLTextAreaElement).value),
        }) as HTMLTextAreaElement)
      : (h('input', {
          type: 'text',
          class:
            'w-full h-9 bg-surface-2 border border-border rounded-md px-3 text-body text-ink-primary focus:border-accent focus:outline-none',
          value: draft[f.name],
          oninput: (e: Event) =>
            (draft[f.name] = (e.target as HTMLInputElement).value),
        }) as HTMLInputElement);

    return h(
      'div',
      { class: 'flex flex-col gap-1' },
      h(
        'label',
        { class: 'text-heading-sm uppercase text-ink-tertiary' },
        f.label
      ),
      el,
      f.hint
        ? h('p', { class: 'text-body-sm text-ink-tertiary' }, f.hint)
        : null
    );
  });

  return h(
    'div',
    { class: 'px-5 pb-4 pt-1 flex flex-col gap-3' },
    ...inputs,
    h(
      'div',
      { class: 'flex items-center justify-end gap-2 mt-1' },
      Button({
        variant: 'primary',
        size: 'sm',
        label: 'Save',
        onClick: async () => {
          const payload: UnknownRecord = {};
          for (const f of spec.fields) {
            const v = draft[f.name].trim();
            if (f.name === 'hashtags') {
              payload[f.name] = v
                .split(/[\s,]+/)
                .map((t) => t.replace(/^#/, '').trim())
                .filter(Boolean);
            } else {
              payload[f.name] = v;
            }
          }
          try {
            await api.updateClip(episodeId, clipId, {
              metadata: { [spec.key]: payload },
            });
            showToast(`${spec.label} metadata saved.`, 'success');
            await reload();
          } catch (e) {
            showToast((e as Error).message, 'error');
          }
        },
      })
    )
  );
}

/* --------------------------------- Chat dock ------------------------------ */

function renderChatDock(
  messages: Signal<ChatMessage[]>,
  sending: Signal<boolean>,
  send: (msg: string) => Promise<void>
): HTMLElement {
  const input = h('textarea', {
    class: [
      'flex-1 bg-transparent text-body text-ink-primary placeholder:text-ink-disabled',
      'resize-none focus:outline-none leading-snug',
    ].join(' '),
    rows: '1',
    placeholder: 'Ask the agent — "rewrite titles around the nuclear angle", "reject clips under 6", "trim clip 3 to 45s"…',
    onkeydown: (e: KeyboardEvent) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        const el = e.target as HTMLTextAreaElement;
        const val = el.value;
        el.value = '';
        send(val);
      }
    },
  }) as HTMLTextAreaElement;

  const log = h('div', {
    class: 'max-h-[28vh] overflow-y-auto px-6 py-3 flex flex-col gap-3',
  });

  effect(() => {
    const msgs = messages();
    if (msgs.length === 0) {
      log.replaceChildren(
        h(
          'p',
          { class: 'text-body-sm text-ink-tertiary italic' },
          'Start a conversation — the agent can retitle clips, adjust hashtags, reject clips by score, and more.'
        )
      );
    } else {
      log.replaceChildren(
        ...msgs.slice(-20).map((m) => chatBubble(m))
      );
      log.scrollTop = log.scrollHeight;
    }
  });

  const sendBtn = h('div');
  effect(() => {
    sendBtn.replaceChildren(
      Button({
        variant: 'primary',
        size: 'md',
        label: sending() ? 'Sending…' : 'Send',
        loading: sending(),
        onClick: () => {
          const val = input.value;
          input.value = '';
          send(val);
        },
      })
    );
  });

  return h(
    'footer',
    {
      class:
        'sticky bottom-0 z-20 border-t border-border-subtle bg-canvas/95 backdrop-blur-md',
    },
    log,
    h(
      'div',
      {
        class:
          'flex items-end gap-3 px-6 py-4 border-t border-border-subtle',
      },
      input,
      sendBtn
    )
  );
}

function chatBubble(m: ChatMessage): HTMLElement {
  const isUser = m.role === 'user';
  return h(
    'div',
    {
      class: `flex ${isUser ? 'justify-end' : 'justify-start'}`,
    },
    h(
      'div',
      {
        class: [
          'max-w-[75%] px-4 py-2.5 rounded-lg text-body leading-relaxed',
          isUser
            ? 'bg-accent text-ink-on-accent'
            : 'bg-surface-2 text-ink-primary border border-border-subtle',
        ].join(' '),
      },
      formatChatContent(m.content),
      m.actions && m.actions.length > 0
        ? h(
            'div',
            { class: 'text-code-sm text-ink-tertiary font-mono tabular mt-2' },
            `${pluralize(m.actions.length, 'action')} executed`
          )
        : null
    )
  );
}

function formatChatContent(content: string): HTMLElement {
  const html = content
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code class="bg-surface-inset px-1 py-0.5 rounded text-code-sm">$1</code>')
    .replace(/\n/g, '<br>');
  return h('span', { html });
}
