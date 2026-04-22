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
import { api, type UnknownRecord } from '../lib/api';
import {
  describeStatus,
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
  {
    key: 'linkedin',
    label: 'LinkedIn',
    fields: [{ name: 'text', label: 'Post text', multiline: true }],
  },
  {
    key: 'facebook',
    label: 'Facebook',
    fields: [{ name: 'caption', label: 'Caption', multiline: true }],
  },
  {
    key: 'threads',
    label: 'Threads',
    fields: [{ name: 'caption', label: 'Caption', multiline: true, hint: 'Max 500 chars' }],
  },
  {
    key: 'pinterest',
    label: 'Pinterest',
    fields: [
      { name: 'title', label: 'Title' },
      { name: 'description', label: 'Description', multiline: true },
    ],
  },
  {
    key: 'bluesky',
    label: 'Bluesky',
    fields: [{ name: 'text', label: 'Post text', multiline: true, hint: 'Max 300 chars' }],
  },
];

interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  actions?: UnknownRecord[];
}

export function ClipReview(target: HTMLElement, episodeId: string): void {
  const clips = signal<UnknownRecord[] | null>(null);
  const episode = signal<UnknownRecord | null>(null);
  const expandedId = signal<string | null>(null);
  const loadError = signal<string | null>(null);
  const chatMessages = signal<ChatMessage[]>([]);
  const chatSending = signal<boolean>(false);

  async function load(): Promise<void> {
    try {
      const [ep, cs] = await Promise.all([
        api.getEpisode(episodeId),
        api.listClips(episodeId),
      ]);
      episode.set(ep);
      clips.set(cs);
      loadError.set(null);
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

    body.replaceChildren(
      h(
        'div',
        { class: 'flex flex-col gap-4 pb-4' },
        ...cs.map((c) =>
          clipCard(episodeId, c, expandedId, async () => load())
        )
      )
    );
  });

  mount(
    target,
    h(
      'div',
      { class: 'min-h-full flex flex-col' },
      renderHeader(episodeId, clips, episode),
      h(
        'div',
        { class: 'flex-1 min-h-0 overflow-y-auto' },
        h('div', { class: 'max-w-[1080px] mx-auto px-10 py-6' }, body)
      ),
      renderChatDock(chatMessages, chatSending, sendChat)
    )
  );
}

function renderHeader(
  episodeId: string,
  clipsSig: Signal<UnknownRecord[] | null>,
  episode: Signal<UnknownRecord | null>
): HTMLElement {
  const title = h('div', {
    class: 'flex-1 min-w-0',
  });

  effect(() => {
    const ep = episode();
    const cs = clipsSig();
    const name = episodeTitle(ep ?? undefined, episodeId);
    const countLine = cs
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
        ep ? StatusPill({ raw: ep.status as string, size: 'sm' }) : null
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
    Button({
      variant: 'primary',
      size: 'md',
      label: 'Approve all',
      onClick: async () => {
        try {
          await api.autoApprove(episodeId);
          showToast(
            'All clips approved — moving to publish.',
            'success'
          );
          navigate(`/episodes/${episodeId}`);
        } catch (e) {
          showToast((e as Error).message, 'error');
        }
      },
    })
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
  reload: () => Promise<void>
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
    class: 'panel overflow-hidden transition-colors duration-[120ms]',
  });

  effect(() => {
    const expanded = expandedId() === id;
    card.classList.toggle('border-border-strong', expanded);

    const head = clipHead(
      episodeId,
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
      () => expandedId.set((prev) => (prev === id ? null : id))
    );
    const children: Node[] = [head];
    if (expanded) {
      children.push(
        clipExpanded(episodeId, id, start, end, metadata, reload)
      );
    }
    card.replaceChildren(...children);
  });

  return card;
}

function clipHead(
  episodeId: string,
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
  toggle: () => void
): HTMLElement {
  return h(
    'div',
    {
      class:
        'p-5 grid grid-cols-[140px_1fr_auto] gap-5 items-start cursor-pointer hover:bg-surface-2/40',
      onclick: toggle,
    },
    clipThumb(episodeId, id, duration),
    h(
      'div',
      { class: 'min-w-0' },
      h(
        'div',
        { class: 'flex items-center gap-2 mb-2 flex-wrap' },
        rank != null
          ? h('span', { class: 'chip font-mono tabular' }, `#${rank}`)
          : null,
        score != null
          ? h('span', { class: 'chip font-mono tabular' }, `${score}/10`)
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
        StatusPill({ descriptor: status, size: 'sm' })
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
            '“' + hook + '”'
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
      },
      Icon.chevronDown()
    )
  );
}

function clipThumb(
  episodeId: string,
  clipId: string,
  duration: number
): HTMLElement {
  const url = `/media/episodes/${episodeId}/shorts/${clipId}.mp4`;
  const video = h('video', {
    src: url,
    muted: true,
    playsinline: true,
    preload: 'none',
    class: 'w-full h-full object-cover bg-surface-inset',
  }) as HTMLVideoElement;

  return h(
    'div',
    {
      class:
        'w-[140px] aspect-[9/16] rounded-md overflow-hidden bg-surface-inset relative',
      onmouseenter: () => video.play().catch(() => {}),
      onmouseleave: () => {
        video.pause();
        video.currentTime = 0;
      },
      onclick: (e: MouseEvent) => e.stopPropagation(),
    },
    video,
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
  reload: () => Promise<void>
): HTMLElement {
  return h(
    'div',
    { class: 'border-t border-border-subtle' },
    renderActions(episodeId, clipId, reload),
    renderTrim(episodeId, clipId, start, end, reload),
    renderMetadataAccordion(episodeId, clipId, metadata, reload)
  );
}

function renderActions(
  episodeId: string,
  clipId: string,
  reload: () => Promise<void>
): HTMLElement {
  return h(
    'div',
    { class: 'flex items-center gap-2 px-5 py-4 flex-wrap' },
    Button({
      variant: 'primary',
      size: 'sm',
      label: 'Keep',
      onClick: async () => {
        try {
          await api.approveClip(episodeId, clipId);
          showToast('Kept.', 'success');
          await reload();
        } catch (e) {
          showToast((e as Error).message, 'error');
        }
      },
    }),
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
          `${countFilled(metadata)} of ${PLATFORMS.length} filled`
        )
      ),
      ...PLATFORMS.map((spec) =>
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

function countFilled(metadata: Record<string, UnknownRecord>): number {
  return PLATFORMS.filter((spec) => {
    const m = metadata[spec.key];
    return m && Object.values(m).some((v) => typeof v === 'string' && v.length > 0);
  }).length;
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
