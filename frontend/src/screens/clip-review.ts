/**
 * Clip Review — the editorial surface.
 *
 * Full-width page. A column of ClipCards (expand-on-click) plus a docked
 * chat input at the bottom that POSTs to /api/episodes/:id/chat. When the
 * agent executes actions that touch clip data, we reload the clip list so
 * the UI reflects the new state.
 */

import { h, mount } from '../lib/dom';
import {
  signal,
  effect,
  effectScope,
  onCleanup,
  type Signal,
} from '../lib/signals';
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
  formatEditableTimecode,
  formatTimecode,
  parseTimecode,
  pluralize,
  type StatusDescriptor,
} from '../lib/format';
import { StatusPill } from '../components/StatusPill';
import { Button } from '../components/Button';
import { EpisodeBackButton } from '../components/EpisodeBackButton';
import { Icon } from '../components/icons';
import { navigate } from '../lib/router';
import {
  displaySpeakerLabel,
  transcriptSpeakerLabels,
} from '../lib/speaker-labels';
import { showToast } from '../state/ui';
import {
  clipApprovalIdentity,
  clipIsApproved,
  matchingClipApprovalFeedback,
  reconcileClipApprovalFeedback,
  saveClipApproval,
  type ClipApprovalFeedback,
} from '../lib/clip-approval';

interface PlatformSpec {
  key: string;
  label: string;
  fields: Array<{ name: string; label: string; multiline?: boolean; hint?: string }>;
}

interface ClipNavigation {
  index: number;
  total: number;
  previousId?: string;
  nextId?: string;
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
  const approvalFeedback = signal<ReadonlyMap<string, ClipApprovalFeedback>>(
    new Map()
  );
  const speakerLabels = signal<Map<number, string>>(new Map());
  const expandedId = signal<string | null>(initialClipId ?? null);
  const setExpanded = (nextId: string | null, focusPlayer = false): void => {
    expandedId.set(nextId);
    const suffix = nextId ? `/${encodeURIComponent(nextId)}` : '';
    window.history.replaceState(
      null,
      '',
      `#/episodes/${episodeId}/clips/review${suffix}`
    );
    if (focusPlayer && nextId) {
      requestAnimationFrame(() => {
        const region = document.getElementById(`clip-review-${nextId}`);
        const player = region?.querySelector<HTMLVideoElement>('video[controls]');
        (player ?? region)?.focus({ preventScroll: true });
      });
    }
  };
  const loadError = signal<string | null>(null);
  const chatMessages = signal<ChatMessage[]>([]);
  const chatSending = signal<boolean>(false);
  let initialClipResolved = false;
  let pollTimer: number | undefined;
  let loadSequence = 0;

  const hasActiveRender = (state: EpisodeReviewState | null): boolean =>
    Boolean(
      state?.clips.some((clip) => clip.review.render_job.status === 'rendering')
    );

  function schedulePoll(state: EpisodeReviewState | null): void {
    if (pollTimer != null) window.clearTimeout(pollTimer);
    pollTimer = undefined;
    if (hasActiveRender(state)) {
      pollTimer = window.setTimeout(() => void load(), 1500);
    }
  }

  async function load(): Promise<void> {
    const sequence = ++loadSequence;
    if (pollTimer != null) window.clearTimeout(pollTimer);
    pollTimer = undefined;
    try {
      const [ep, state] = await Promise.all([
        api.getEpisode(episodeId),
        api.review(episodeId),
      ]);
      if (sequence !== loadSequence) return;
      const reconciledFeedback = reconcileClipApprovalFeedback(
        approvalFeedback.peek(),
        state.clips
      );
      episode.set(ep);
      review.set(state);
      clips.set(state.clips);
      approvalFeedback.set(reconciledFeedback);
      if (!initialClipResolved) {
        initialClipResolved = true;
        const currentId = expandedId.peek();
        const currentExists = state.clips.some(
          (clip) => String(clip.id ?? clip.clip_id) === currentId
        );
        const firstPlayable = playbackClipIds(state.clips)[0];
        if (!currentExists && firstPlayable) setExpanded(firstPlayable);
      }
      loadError.set(null);
      schedulePoll(state);
    } catch (e) {
      if (sequence !== loadSequence) return;
      loadError.set((e as Error).message);
      schedulePoll(review.peek());
    }
  }

  onCleanup(() => {
    loadSequence += 1;
    if (pollTimer != null) window.clearTimeout(pollTimer);
  });

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

  async function loadSpeakerLabels(): Promise<void> {
    try {
      const transcript = await api.getTranscript(episodeId);
      speakerLabels.set(transcriptSpeakerLabels(transcript.speaker_map));
    } catch {
      /* A missing transcript keeps neutral speaker numbers visible. */
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
  void loadSpeakerLabels();

  const body = h('div');
  const clipList = h('div', { class: 'flex flex-col gap-4 pb-4' });
  const scrollViewport = h(
    'div',
    { class: 'flex-1 min-h-0 overflow-y-auto' },
    h(
      'div',
      { class: 'max-w-[1080px] mx-auto px-4 sm:px-10 py-6 pb-32' },
      body
    )
  );
  const revealExpanded = (region: HTMLElement): void => {
    requestAnimationFrame(() => {
      const offset =
        region.getBoundingClientRect().top -
        scrollViewport.getBoundingClientRect().top;
      scrollViewport.scrollTop += offset;
    });
  };
  const cardEntries = new Map<
    string,
    { element: HTMLElement; signature: string; dispose: () => void }
  >();
  onCleanup(() => {
    for (const entry of cardEntries.values()) entry.dispose();
    cardEntries.clear();
  });

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
      for (const entry of cardEntries.values()) entry.dispose();
      cardEntries.clear();
      clipList.replaceChildren();
      body.replaceChildren(emptyClipsPanel());
      return;
    }

    const state = review();
    const labels = speakerLabels();
    const platforms = enabledPlatforms(state?.enabled_destinations ?? []);
    const playableIds = playbackClipIds(cs);
    const desired: HTMLElement[] = [];
    const present = new Set<string>();

    for (const clip of cs) {
      const id = String(clip.id ?? clip.clip_id);
      const speaker = displaySpeakerLabel(clip.speaker, labels);
      present.add(id);
      const signature = JSON.stringify({
        clip,
        speaker,
        platforms: platforms.map((platform) => platform.key),
        playableIds,
      });
      let entry = cardEntries.get(id);
      if (!entry || entry.signature !== signature) {
        const next = clipCard(
          episodeId,
          clip,
          speaker,
          expandedId,
          clip.review as ClipReviewState,
          approvalFeedback,
          platforms,
          async () => load(),
          setExpanded,
          clipNavigation(playableIds, id),
          revealExpanded
        );
        if (entry?.element.parentNode === clipList) {
          entry.element.replaceWith(next.element);
        }
        entry?.dispose();
        entry = { ...next, signature };
        cardEntries.set(id, entry);
      }
      desired.push(entry.element);
    }

    for (const [id, entry] of cardEntries) {
      if (present.has(id)) continue;
      entry.element.remove();
      entry.dispose();
      cardEntries.delete(id);
    }

    desired.forEach((element, index) => {
      if (clipList.children[index] !== element) {
        clipList.insertBefore(element, clipList.children[index] ?? null);
      }
    });
    while (clipList.children.length > desired.length) {
      clipList.lastElementChild?.remove();
    }
    if (body.firstElementChild !== clipList) body.replaceChildren(clipList);
  });

  mount(
    target,
    h(
      'div',
      { class: 'h-full min-h-0 flex flex-col overflow-hidden' },
      renderHeader(episodeId, clips, episode, review),
      scrollViewport,
      renderChatDock(chatMessages, chatSending, sendChat)
    )
  );
}

function playbackClipIds(clips: UnknownRecord[]): string[] {
  const playable = clips.filter(
    (clip) => (clip.review as ClipReviewState).render.playable
  );
  const current = playable.filter(
    (clip) => (clip.review as ClipReviewState).render.current
  );
  return (current.length ? current : playable).map((clip) =>
    String(clip.id ?? clip.clip_id)
  );
}

function clipNavigation(ids: string[], id: string): ClipNavigation | undefined {
  const index = ids.indexOf(id);
  if (index < 0) return undefined;
  return {
    index,
    total: ids.length,
    previousId: ids[index - 1],
    nextId: ids[index + 1],
  };
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
    EpisodeBackButton(episodeId),
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
  speaker: string | null,
  expandedId: Signal<string | null>,
  review: ClipReviewState,
  approvalFeedback: Signal<ReadonlyMap<string, ClipApprovalFeedback>>,
  platforms: PlatformSpec[],
  reload: () => Promise<void>,
  setExpanded: (clipId: string | null, focusPlayer?: boolean) => void,
  navigation: ClipNavigation | undefined,
  revealExpanded: (region: HTMLElement) => void
): { element: HTMLElement; dispose: () => void } {
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
  const status = describeStatus((clip.status as string) ?? 'pending');
  const metadata = (clip.metadata as Record<string, UnknownRecord>) ?? {};

  const card = h('article', {
    id: `clip-${id}`,
    class:
      'panel scroll-mt-24 overflow-hidden transition-colors duration-[120ms]',
  });
  let expandedScope: { element: HTMLElement; dispose: () => void } | null = null;

  const dispose = effectScope(() => {
    onCleanup(() => expandedScope?.dispose());
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
        () => setExpanded(expanded ? null : id, !expanded)
      );
      expandedScope?.dispose();
      expandedScope = null;
      const children: Node[] = [head];
      if (expanded) {
        let element!: HTMLElement;
        const disposeExpanded = effectScope(() => {
          element = clipExpanded(
            episodeId,
            id,
            start,
            end,
            metadata,
            review,
            clip,
            approvalFeedback,
            platforms,
            reload,
            navigation,
            setExpanded
          );
        });
        expandedScope = { element, dispose: disposeExpanded };
        children.push(element);
      }
      card.replaceChildren(...children);
      if (expanded && expandedScope) revealExpanded(expandedScope.element);
    });
  });

  return { element: card, dispose };
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
  speaker: string | null,
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
      'aria-label': `${expanded ? 'Collapse' : render.playable ? 'Watch' : 'Review'} ${title}`,
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
  if (render.playable && render.url) {
    const url = render.url;
    const video = h('video', {
      src: url,
      playsinline: true,
      preload: 'metadata',
      tabindex: '-1',
      'aria-hidden': 'true',
      class: 'w-full h-full object-cover bg-surface-inset pointer-events-none',
    }) as HTMLVideoElement;
    video.muted = true;
    innerEl = video;
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
    },
    innerEl,
    render.playable
      ? h(
          'span',
          {
            class:
              'absolute bottom-1 left-1 inline-flex items-center gap-1 text-code-sm text-white bg-black/75 rounded px-1.5 py-0.5',
          },
          Icon.play({ size: 11 }),
          'Watch'
        )
      : null,
    h(
      'div',
      {
        class:
          'absolute top-1 right-1 text-code-sm text-ink-primary font-mono tabular bg-black/60 rounded px-1.5 py-0.5',
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
  clip: UnknownRecord,
  approvalFeedback: Signal<ReadonlyMap<string, ClipApprovalFeedback>>,
  platforms: PlatformSpec[],
  reload: () => Promise<void>,
  navigation: ClipNavigation | undefined,
  setExpanded: (clipId: string | null, focusPlayer?: boolean) => void
): HTMLElement {
  return h(
    'div',
    {
      id: `clip-review-${clipId}`,
      class: 'border-t border-border-subtle',
      tabindex: '-1',
    },
    renderReviewPlayer(clipId, review.render, navigation, setExpanded),
    renderActions(
      episodeId,
      clipId,
      clip,
      review,
      approvalFeedback,
      reload
    ),
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
  render: ReviewArtifact,
  navigation: ClipNavigation | undefined,
  setExpanded: (clipId: string | null, focusPlayer?: boolean) => void
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
        'px-5 py-4 bg-surface-inset/50 border-b border-border-subtle',
      'aria-label': 'Clip video review',
    },
    navigation ? renderPlaybackNavigation(navigation, setExpanded) : null,
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
      {
        class: 'mx-auto max-w-full',
        style: { width: 'min(390px, 30dvh)' },
      },
      h('video', {
        src: url,
        controls: true,
        playsinline: true,
        preload: 'metadata',
        class:
          'block w-full aspect-[9/16] object-contain bg-black rounded-lg border border-border-strong shadow-lift-lg',
        'aria-label': `Review video for ${clipId}`,
      }),
      h(
        'div',
        { class: 'flex items-center justify-between gap-3 mt-2' },
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

function renderPlaybackNavigation(
  navigation: ClipNavigation,
  setExpanded: (clipId: string | null, focusPlayer?: boolean) => void
): HTMLElement {
  return h(
    'nav',
    {
      class: 'max-w-[720px] mx-auto mb-3 flex items-center justify-between gap-3',
      'aria-label': 'Rendered clips',
    },
    Button({
      variant: 'secondary',
      size: 'sm',
      label: 'Previous',
      icon: Icon.chevronLeft({ size: 14 }),
      disabled: !navigation.previousId,
      onClick: () =>
        navigation.previousId && setExpanded(navigation.previousId, true),
    }),
    h(
      'span',
      { class: 'text-body-sm text-ink-secondary font-mono tabular' },
      `Clip ${navigation.index + 1} of ${navigation.total}`
    ),
    Button({
      variant: 'secondary',
      size: 'sm',
      label: 'Next',
      iconRight: Icon.chevronRight({ size: 14 }),
      disabled: !navigation.nextId,
      onClick: () => navigation.nextId && setExpanded(navigation.nextId, true),
    })
  );
}

function renderActions(
  episodeId: string,
  clipId: string,
  clip: UnknownRecord,
  review: ClipReviewState,
  approvalFeedback: Signal<ReadonlyMap<string, ClipApprovalFeedback>>,
  reload: () => Promise<void>
): HTMLElement {
  const rendering = signal(review.render_job.status === 'rendering');
  const primary = h('span');
  const feedbackHost = h('p', {
    class: 'basis-full text-body-sm min-h-5',
    'aria-live': 'polite',
  });
  effect(() => {
    const active = rendering();
    const feedback = matchingClipApprovalFeedback(
      clip,
      approvalFeedback().get(clipId)
    );
    const saving = feedback?.status === 'saving';
    const approved = clipIsApproved(clip, feedback);
    const failed = feedback?.status === 'error';
    primary.replaceChildren(
      Button({
        variant: 'primary',
        size: 'sm',
        label: saving
          ? 'Saving…'
          : approved
            ? 'Approved'
            : review.render.current
              ? failed
                ? 'Retry approval'
                : 'Final approve'
              : active
                ? 'Rendering…'
                : review.render.playable
                  ? 'Re-render clip'
                  : 'Render clip',
        disabled: approved || active || saving,
        loading: active || saving,
        onClick: async () => {
          if (review.render.current) {
            const identity = clipApprovalIdentity(clip);
            const outcome = await saveClipApproval(
              identity,
              () => api.approveClip(episodeId, clipId),
              (next) => {
                approvalFeedback.set((current) => {
                  const updated = new Map(current);
                  updated.set(clipId, next);
                  return updated;
                });
              }
            );
            if (outcome.status === 'error') {
              showToast(outcome.message ?? 'Could not approve clip', 'error');
              return;
            }
            showToast('Current render approved.', 'success');
            await reload();
            return;
          }
          try {
            rendering.set(true);
            await api.selectClip(episodeId, clipId);
            showToast('Rendering the selected clip locally…');
            await api.renderClip(episodeId, clipId);
            showToast('Clip rendered. Review it before final approval.', 'success');
            await reload();
          } catch (e) {
            showToast((e as Error).message, 'error');
          } finally {
            rendering.set(false);
          }
        },
      })
    );
    feedbackHost.className = `basis-full text-body-sm min-h-5 ${
      failed
        ? 'text-status-danger'
        : saving
          ? 'text-ink-secondary'
          : approved
            ? 'text-status-success'
            : 'text-ink-tertiary'
    }`;
    if (failed) {
      feedbackHost.setAttribute('role', 'alert');
      feedbackHost.textContent = `Approval failed: ${feedback?.message ?? 'Could not approve clip'}`;
    } else {
      feedbackHost.removeAttribute('role');
      feedbackHost.textContent = saving
        ? 'Saving approval…'
        : approved
          ? 'Approval saved for this render.'
          : '';
    }
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
    }),
    feedbackHost
  );
}

function renderTrim(
  episodeId: string,
  clipId: string,
  initialStart: number,
  initialEnd: number,
  reload: () => Promise<void>
): HTMLElement {
  const initialStartText = formatEditableTimecode(initialStart);
  const initialEndText = formatEditableTimecode(initialEnd);
  const startStr = signal<string>(initialStartText);
  const endStr = signal<string>(initialEndText);

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
        const startText = startStr.peek().trim();
        const endText = endStr.peek().trim();
        const s =
          startText === initialStartText ? initialStart : parseTimecode(startText);
        const e =
          endText === initialEndText ? initialEnd : parseTimecode(endText);
        if (s == null || e == null || e <= s) {
          showToast(
            'Use finite, non-negative times such as 21:06.080, with seconds below 60.',
            'error'
          );
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
  'w-32 h-9 bg-surface-2 border border-border rounded-md px-2.5 text-body text-ink-primary font-mono tabular focus:border-accent focus:outline-none';

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
