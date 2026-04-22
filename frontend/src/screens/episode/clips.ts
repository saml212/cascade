import { h } from '../../lib/dom';
import { Button } from '../../components/Button';
import { navigate } from '../../lib/router';
import { formatDuration, pluralize } from '../../lib/format';

export function renderClips(
  target: HTMLElement,
  ep: Record<string, unknown>,
  episodeId: string
): void {
  const clips = ((ep.clips as Array<Record<string, unknown>>) ?? []).slice();
  // Sort by rank if available, else by start time, to mirror the review surface
  clips.sort((a, b) => {
    const ra = (a.rank as number) ?? 99;
    const rb = (b.rank as number) ?? 99;
    if (ra !== rb) return ra - rb;
    return ((a.start_seconds as number) ?? 0) - ((b.start_seconds as number) ?? 0);
  });

  if (clips.length === 0) {
    target.replaceChildren(
      h(
        'div',
        { class: 'panel p-16 text-center' },
        h(
          'div',
          {
            class:
              'font-display text-display-md text-ink-secondary mb-3',
          },
          'Clips haven’t been mined yet.'
        ),
        h(
          'p',
          { class: 'text-body text-ink-tertiary max-w-md mx-auto' },
          'The clip miner runs after longform approval. Once it finishes, clips will appear here for review.'
        )
      )
    );
    return;
  }

  const approved = clips.filter(
    (c) => c.status === 'approved' || c.status === 'published'
  ).length;
  const rejected = clips.filter((c) => c.status === 'rejected').length;
  const pending = clips.length - approved - rejected;

  target.replaceChildren(
    h(
      'div',
      { class: 'flex flex-col gap-6' },
      // Header card — counts + CTA
      h(
        'div',
        { class: 'panel p-6 flex items-center justify-between flex-wrap gap-4' },
        h(
          'div',
          { class: 'flex items-center gap-8' },
          statBlock('Total', String(clips.length), 'ink-primary'),
          statBlock('Kept', String(approved), 'status-success'),
          statBlock('Pending', String(pending), pending > 0 ? 'status-warning' : 'ink-secondary'),
          statBlock('Rejected', String(rejected), 'ink-secondary')
        ),
        Button({
          variant: 'primary',
          size: 'lg',
          label: 'Open editorial review',
          onClick: () => navigate(`/episodes/${episodeId}/clips/review`),
        })
      ),
      // Thumbnail strip
      h(
        'div',
        { class: 'panel p-5' },
        h(
          'div',
          { class: 'flex items-baseline justify-between mb-3' },
          h(
            'span',
            { class: 'text-heading-sm uppercase text-ink-tertiary' },
            `${pluralize(clips.length, 'clip')} in order`
          ),
          h(
            'span',
            { class: 'text-body-sm text-ink-tertiary' },
            'Hover a tile to preview. Click to open the review surface.'
          )
        ),
        h(
          'div',
          {
            class:
              'grid gap-3',
            style: {
              gridTemplateColumns:
                'repeat(auto-fill, minmax(132px, 1fr))',
            },
          },
          ...clips.map((c) => renderTile(c, episodeId))
        )
      )
    )
  );
}

function statBlock(label: string, value: string, tone: string): HTMLElement {
  return h(
    'div',
    null,
    h(
      'div',
      { class: 'text-heading-sm uppercase text-ink-tertiary mb-1' },
      label
    ),
    h(
      'div',
      {
        class: `text-display-md font-display text-${tone} font-mono tabular leading-none`,
      },
      value
    )
  );
}

function renderTile(
  clip: Record<string, unknown>,
  episodeId: string
): HTMLElement {
  const id = (clip.id as string) ?? (clip.clip_id as string);
  const title = (clip.title as string) || 'Untitled';
  const duration = (clip.duration as number) ?? 0;
  const rank = (clip.rank as number) ?? null;
  const score = (clip.virality_score as number) ?? null;
  const status = (clip.status as string) ?? 'pending';
  const url = `/media/episodes/${episodeId}/shorts/${id}.mp4`;

  const video = h('video', {
    src: url,
    muted: true,
    playsinline: true,
    preload: 'none',
    class: 'w-full h-full object-cover',
  }) as HTMLVideoElement;

  const statusTone =
    status === 'approved' || status === 'published'
      ? 'bg-status-success'
      : status === 'rejected'
      ? 'bg-status-danger'
      : 'bg-status-warning';

  return h(
    'button',
    {
      onclick: () => navigate(`/episodes/${episodeId}/clips/review`),
      onmouseenter: () => video.play().catch(() => {}),
      onmouseleave: () => {
        video.pause();
        video.currentTime = 0;
      },
      class:
        'group text-left flex flex-col gap-1.5 focus:outline-none',
    },
    h(
      'div',
      {
        class:
          'relative aspect-[9/16] w-full rounded-md overflow-hidden bg-surface-inset border border-border-subtle group-hover:border-border-strong transition-colors',
      },
      video,
      h(
        'div',
        {
          class: 'absolute top-1.5 left-1.5 flex items-center gap-1.5',
        },
        rank != null
          ? h(
              'span',
              {
                class:
                  'text-code-sm text-ink-primary font-mono tabular bg-black/70 rounded px-1.5 py-0.5',
              },
              `#${rank}`
            )
          : null,
        h('span', {
          class: `w-2 h-2 rounded-full ${statusTone} shadow-[0_0_4px_rgba(0,0,0,0.4)]`,
        })
      ),
      h(
        'div',
        {
          class:
            'absolute bottom-1.5 right-1.5 text-code-sm text-ink-primary font-mono tabular bg-black/70 rounded px-1.5 py-0.5',
        },
        formatDuration(duration)
      )
    ),
    h(
      'div',
      { class: 'min-w-0' },
      h(
        'div',
        { class: 'text-body-sm text-ink-primary font-medium line-clamp-2' },
        title
      ),
      score != null
        ? h(
            'div',
            { class: 'text-code-sm text-ink-tertiary font-mono tabular mt-0.5' },
            `${score}/10`
          )
        : null
    )
  );
}
