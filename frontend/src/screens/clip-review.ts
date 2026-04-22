import { h, mount } from '../lib/dom';
import { signal, effect } from '../lib/signals';
import { api, type UnknownRecord } from '../lib/api';
import { describeStatus, formatDuration, formatTimecode, pluralize } from '../lib/format';
import { StatusPill } from '../components/StatusPill';
import { Button } from '../components/Button';
import { Icon } from '../components/icons';
import { link, navigate } from '../lib/router';
import { showToast } from '../state/ui';

export function ClipReview(target: HTMLElement, episodeId: string): void {
  const clips = signal<UnknownRecord[] | null>(null);
  const episode = signal<UnknownRecord | null>(null);
  const expandedId = signal<string | null>(null);
  const loadError = signal<string | null>(null);

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

  void load();

  const body = h('div');

  effect(() => {
    const cs = clips();
    const ep = episode();
    const err = loadError();

    if (err && !cs) {
      body.replaceChildren(
        h(
          'div',
          {
            class:
              'panel p-8 text-body text-status-danger border-status-danger/30',
          },
          err
        )
      );
      return;
    }
    if (!cs || !ep) {
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
      body.replaceChildren(
        h(
          'div',
          { class: 'panel p-16 text-center' },
          h(
            'div',
            {
              class:
                'font-display text-display-md text-ink-secondary mb-3',
            },
            'No clips mined yet.'
          ),
          h(
            'p',
            { class: 'text-body text-ink-tertiary max-w-md mx-auto' },
            'The clip miner runs after longform approval. Come back once it’s finished.'
          )
        )
      );
      return;
    }

    body.replaceChildren(
      h(
        'div',
        { class: 'flex flex-col gap-4' },
        ...cs.map((c) => clipCard(episodeId, c, expandedId, async () => load()))
      )
    );
  });

  mount(
    target,
    h(
      'div',
      { class: 'min-h-full' },
      h(
        'header',
        {
          class:
            'sticky top-0 z-10 bg-canvas/90 backdrop-blur-sm border-b border-border-subtle px-10 py-5',
        },
        h(
          'div',
          { class: 'max-w-[1200px] mx-auto flex items-center gap-4' },
          h(
            'a',
            {
              ...link(`/episodes/${episodeId}`),
              class:
                'w-8 h-8 flex items-center justify-center rounded-md text-ink-tertiary hover:text-ink-primary hover:bg-surface-2',
            },
            Icon.chevronLeft()
          ),
          h(
            'div',
            { class: 'flex-1' },
            h(
              'div',
              { class: 'font-display text-display-md' },
              'Clip review'
            ),
            effect_text(episodeId, clips)
          ),
          Button({
            variant: 'primary',
            size: 'md',
            label: 'Approve all',
            onClick: async () => {
              try {
                await api.autoApprove(episodeId);
                showToast('All clips approved — moving to publish.', 'success');
                navigate(`/episodes/${episodeId}`);
              } catch (e) {
                showToast((e as Error).message, 'error');
              }
            },
          })
        )
      ),
      h('div', { class: 'max-w-[1200px] mx-auto px-10 py-8' }, body)
    )
  );
}

function effect_text(
  episodeId: string,
  clips: ReturnType<typeof signal<UnknownRecord[] | null>>
): HTMLElement {
  const span = h('div', { class: 'text-body-sm text-ink-tertiary mt-1' });
  effect(() => {
    const cs = clips();
    span.textContent = cs
      ? `${pluralize(cs.length, 'clip')} · ${episodeId}`
      : episodeId;
  });
  return span;
}

function clipCard(
  episodeId: string,
  clip: UnknownRecord,
  expandedId: ReturnType<typeof signal<string | null>>,
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

  const card = h('article', {
    class:
      'panel overflow-hidden transition-colors duration-[120ms] hover:border-border-strong',
  });

  effect(() => {
    const expanded = expandedId() === id;
    const head = h(
        'div',
        {
          class: 'p-6 grid grid-cols-[160px_1fr_auto] gap-6 items-start cursor-pointer',
          onclick: () =>
            expandedId.set((prev) => (prev === id ? null : id)),
        },
        thumb(episodeId, id, duration),
        h(
          'div',
          { class: 'min-w-0' },
          h(
            'div',
            { class: 'flex items-center gap-3 mb-2 flex-wrap' },
            rank != null
              ? h(
                  'span',
                  { class: 'chip font-mono tabular' },
                  `#${rank}`
                )
              : null,
            score != null
              ? h(
                  'span',
                  { class: 'chip font-mono tabular' },
                  `${score}/10`
                )
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
            speaker
              ? h('span', { class: 'chip' }, speaker)
              : null,
            StatusPill({ descriptor: status, size: 'sm' })
          ),
          h('h3', { class: 'text-heading-lg text-ink-primary' }, title),
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
          'div',
          { class: 'flex flex-col items-end gap-2' },
          h(
            'span',
            {
              class: `text-ink-tertiary transition-transform duration-[200ms] ${expanded ? 'rotate-180' : ''}`,
            },
            Icon.chevronDown()
          )
        )
    );
    const children: Node[] = [head];
    if (expanded) children.push(expandedActions(episodeId, id, reload));
    card.replaceChildren(...children);
  });

  return card;
}

function thumb(
  episodeId: string,
  clipId: string,
  duration: number
): HTMLElement {
  const url = `/media/episodes/${episodeId}/shorts/${clipId}.mp4`;
  const video = h('video', {
    src: url,
    muted: true,
    playsinline: true,
    class: 'w-full h-full object-cover',
  }) as HTMLVideoElement;

  const wrap = h(
    'div',
    {
      class:
        'w-[160px] aspect-[9/16] rounded-md overflow-hidden bg-surface-inset relative group',
      onmouseenter: () => video.play().catch(() => {}),
      onmouseleave: () => {
        video.pause();
        video.currentTime = 0;
      },
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
  return wrap;
}

function expandedActions(
  episodeId: string,
  clipId: string,
  reload: () => Promise<void>
): HTMLElement {
  return h(
    'div',
    {
      class: 'border-t border-border-subtle px-6 py-4 flex items-center gap-2',
    },
    Button({
      variant: 'primary',
      size: 'sm',
      label: 'Keep',
      onClick: async () => {
        await api.approveClip(episodeId, clipId);
        showToast('Kept.', 'success');
        await reload();
      },
    }),
    Button({
      variant: 'destructive',
      size: 'sm',
      label: 'Reject',
      onClick: async () => {
        await api.rejectClip(episodeId, clipId);
        showToast('Rejected.');
        await reload();
      },
    }),
    Button({
      variant: 'ghost',
      size: 'sm',
      label: 'Retitle',
      onClick: () => showToast('Retitle via chat coming next.'),
    }),
    Button({
      variant: 'ghost',
      size: 'sm',
      label: 'Trim',
      onClick: () => showToast('Inline trim coming next.'),
    }),
    Button({
      variant: 'ghost',
      size: 'sm',
      label: 'Per-platform metadata',
      onClick: () => showToast('Metadata accordion coming next.'),
    })
  );
}
