import { Button } from '../../components/Button';
import { Icon } from '../../components/icons';
import { h } from '../../lib/dom';
import {
  api,
  type ClipReviewState,
  type EpisodeReviewState,
  type UnknownRecord,
} from '../../lib/api';
import { formatDuration, pluralize } from '../../lib/format';
import { currentPath, navigate } from '../../lib/router';

export function renderClips(
  target: HTMLElement,
  _ep: Record<string, unknown>,
  episodeId: string
): void {
  target.replaceChildren(
    h('div', { class: 'panel p-12 text-center text-ink-tertiary' }, 'Loading review state…')
  );
  void api
    .review(episodeId)
    .then((review) => {
      if (currentPath() === `/episodes/${episodeId}/clips`) {
        renderClipState(target, episodeId, review);
      }
    })
    .catch((error: Error) => {
      if (currentPath() === `/episodes/${episodeId}/clips`) {
        target.replaceChildren(
          h(
            'div',
            { class: 'panel p-8 text-status-danger border-status-danger/30' },
            `Could not load review state: ${error.message}`
          )
        );
      }
    });
}

function renderClipState(
  target: HTMLElement,
  episodeId: string,
  review: EpisodeReviewState
): void {
  const clips = review.clips.slice().sort((left, right) => {
    const leftRank = (left.rank as number) ?? 99;
    const rightRank = (right.rank as number) ?? 99;
    return leftRank !== rightRank
      ? leftRank - rightRank
      : Number(left.start_seconds ?? 0) - Number(right.start_seconds ?? 0);
  });

  if (clips.length === 0) {
    target.replaceChildren(
      h(
        'div',
        { class: 'panel p-16 text-center' },
        h(
          'div',
          { class: 'font-display text-display-md text-ink-secondary mb-3' },
          'Clips haven’t been mined yet.'
        ),
        h(
          'p',
          { class: 'text-body text-ink-tertiary max-w-md mx-auto' },
          'The clip miner runs after longform approval. Candidates appear here for selection and review.'
        )
      )
    );
    return;
  }

  const selected = clips.filter(
    (clip) => clip.review.selection.status === 'selected'
  );
  const currentRenders = selected.filter((clip) => clip.review.render.current);
  const finalApproved = selected.filter((clip) => clip.review.approval.current);
  const renderNeeded = selected.filter((clip) => !clip.review.render.current);
  const previousPlayable = selected.filter(
    (clip) => clip.review.render.playable && !clip.review.render.current
  );
  const playableClips = clips.filter((clip) => clip.review.render.playable);
  const currentClips = playableClips.filter((clip) => clip.review.render.current);
  const playable = currentClips.length ? currentClips : playableClips;
  const firstPlayableId = playable[0]
    ? String(playable[0].id ?? playable[0].clip_id)
    : null;

  target.replaceChildren(
    h(
      'div',
      { class: 'flex flex-col gap-6' },
      h(
        'div',
        { class: 'panel p-6 flex items-center justify-between flex-wrap gap-4' },
        h(
          'div',
          { class: 'flex items-center gap-8 flex-wrap' },
          statBlock('Candidates', String(review.clip_summary.candidate_count), 'ink-primary'),
          statBlock('Selected', String(review.clip_summary.selected_count), 'ink-primary'),
          statBlock('Current renders', String(currentRenders.length), 'ink-primary'),
          statBlock('Final approved', String(finalApproved.length), 'status-success'),
          statBlock('Rejected', String(review.clip_summary.rejected_count), 'ink-secondary')
        ),
        Button({
          variant: 'primary',
          size: 'lg',
          label: playable.length
            ? `Watch ${pluralize(playable.length, 'clip')}`
            : 'Open editorial review',
          icon: playable.length ? Icon.play({ size: 18 }) : undefined,
          onClick: () =>
            navigate(
              firstPlayableId
                ? `/episodes/${episodeId}/clips/review/${encodeURIComponent(firstPlayableId)}`
                : `/episodes/${episodeId}/clips/review`
            ),
        })
      ),
      h(
        'div',
        { class: 'panel p-5' },
        h(
          'div',
          { class: 'flex items-baseline justify-between mb-3' },
          h(
            'span',
            { class: 'text-heading-sm uppercase text-ink-tertiary' },
            `${pluralize(clips.length, 'candidate')} in editorial order`
          ),
          h(
            'span',
            { class: 'text-body-sm text-ink-tertiary' },
            'Choose a rendered clip to watch with sound and full playback controls.'
          )
        ),
        renderNeeded.length > 0
          ? h(
              'div',
              {
                class:
                  'mb-4 px-4 py-3 rounded-md bg-status-warning/10 border border-status-warning/30 text-body-sm text-ink-secondary',
                role: 'status',
              },
              `${renderNeeded.length} selected ${pluralize(renderNeeded.length, 'clip')} ${
                renderNeeded.length === 1 ? 'needs' : 'need'
              } a current render.`,
              previousPlayable.length > 0
                ? ` ${previousPlayable.length} previous ${pluralize(previousPlayable.length, 'file')} remain reviewable.`
                : '',
              ' Final approval stays locked until current files are reviewed.'
            )
          : null,
        h(
          'div',
          {
            class: 'grid gap-3',
            style: { gridTemplateColumns: 'repeat(auto-fill, minmax(132px, 1fr))' },
          },
          ...clips.map((clip) => renderTile(clip, episodeId))
        )
      )
    )
  );
}

function statBlock(label: string, value: string, tone: string): HTMLElement {
  return h(
    'div',
    null,
    h('div', { class: 'text-heading-sm uppercase text-ink-tertiary mb-1' }, label),
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
  clip: UnknownRecord & { review: ClipReviewState },
  episodeId: string
): HTMLElement {
  const id = String(clip.id ?? clip.clip_id);
  const title = (clip.title as string) || 'Untitled';
  const start = Number(clip.start_seconds ?? 0);
  const end = Number(clip.end_seconds ?? start);
  const duration = Number(clip.duration ?? end - start);
  const rank = (clip.rank as number) ?? null;
  const score = (clip.virality_score as number) ?? null;
  const manual = Boolean(clip.manual);
  const state = clip.review;
  const statusTone = state.approval.current
    ? 'bg-status-success'
    : state.selection.status === 'rejected'
      ? 'bg-status-danger'
      : state.selection.status === 'selected'
        ? 'bg-status-warning'
        : 'bg-ink-tertiary';

  let preview: HTMLElement;
  if (state.render.playable && state.render.url) {
    const video = h('video', {
      src: state.render.url,
      playsinline: true,
      preload: 'metadata',
      tabindex: '-1',
      'aria-hidden': 'true',
      class: 'w-full h-full object-cover pointer-events-none',
    }) as HTMLVideoElement;
    video.muted = true;
    preview = video;
  } else {
    preview = h('div', {
      class: 'w-full h-full bg-surface-inset flex items-center justify-center',
    });
  }

  return h(
    'button',
    {
      type: 'button',
      onclick: () =>
        navigate(`/episodes/${episodeId}/clips/review/${encodeURIComponent(id)}`),
      class: 'group text-left flex flex-col gap-1.5 focus:outline-none',
      'aria-label': `${state.render.playable ? 'Watch' : 'Review'} ${title}`,
    },
    h(
      'div',
      {
        class:
          'relative aspect-[9/16] w-full rounded-md overflow-hidden bg-surface-inset border border-border-subtle group-hover:border-border-strong group-focus-visible:border-accent group-focus-visible:ring-2 group-focus-visible:ring-accent/40 transition-colors',
      },
      preview,
      h(
        'div',
        { class: 'absolute top-1.5 left-1.5 flex items-center gap-1.5' },
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
      state.render.playable
        ? h(
            'span',
            {
              class:
                'absolute top-1.5 right-1.5 text-code-sm text-white bg-black/70 rounded px-1.5 py-0.5',
            },
            state.render.current ? 'Current' : 'Previous'
          )
        : null,
      state.render.playable
        ? h(
            'span',
            {
              class:
                'absolute bottom-1.5 left-1.5 inline-flex items-center gap-1 text-code-sm text-white bg-black/75 rounded px-1.5 py-0.5',
            },
            Icon.play({ size: 11 }),
            'Watch'
          )
        : null,
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
      score != null && !(manual && score === 0)
        ? h(
            'div',
            { class: 'text-code-sm text-ink-tertiary font-mono tabular mt-0.5' },
            `Candidate score ${score}/10`
          )
        : manual
          ? h(
              'div',
              { class: 'text-code-sm text-ink-tertiary mt-0.5' },
              'Manual selection · unscored'
            )
          : null
    )
  );
}
