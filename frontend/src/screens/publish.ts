/** Publish review for the current, revision-bound release state. */

import { Button } from '../components/Button';
import { EpisodeBackButton } from '../components/EpisodeBackButton';
import { StatusPill } from '../components/StatusPill';
import {
  api,
  type EpisodeReviewState,
  type QualitySnapshot,
  type ReviewDestination,
  type UnknownRecord,
} from '../lib/api';
import { h, mount } from '../lib/dom';
import {
  clipDistributionLabel,
  clipDistributionReady,
} from '../lib/clip-review-surface';
import {
  describeEpisodeStatus,
  episodeDisplayDuration,
  episodeTitle,
  formatDuration,
  pluralize,
  type StatusDescriptor,
} from '../lib/format';
import { navigate } from '../lib/router';
import { effect, signal, type Signal } from '../lib/signals';
import {
  episodeDetail,
  episodeDetailError,
} from '../state/episodes';
import { showToast } from '../state/ui';

type ReviewedClip = EpisodeReviewState['clips'][number];

export const DESTINATION_COLORS: Record<string, string> = {
  youtube: '#ff3344',
  tiktok: '#69c9d0',
  instagram: '#e1306c',
  x: '#e8e8e8',
  facebook: '#1877f2',
  threads: '#f5f5f5',
  bluesky: '#1684ff',
  linkedin: '#0a66c2',
  pinterest: '#e60023',
};

export function Publish(target: HTMLElement, episodeId: string): void {
  const review = signal<EpisodeReviewState | null>(null);
  const loadError = signal<string | null>(null);
  const publishing = signal(false);

  void api
    .review(episodeId)
    .then((state) => {
      review.set(state);
      loadError.set(null);
    })
    .catch((error: Error) => loadError.set(error.message));

  const page = h('div', { class: 'min-h-full flex flex-col' });
  effect(() => {
    const episode = episodeDetail();
    const state = review();
    const error = loadError() ?? episodeDetailError();

    if (error && (!episode || !state)) {
      page.replaceChildren(
        h('div', { class: 'px-10 py-10 text-status-danger' }, error)
      );
      return;
    }
    if (!episode || !state) {
      page.replaceChildren(
        h(
          'div',
          { class: 'px-10 py-10' },
          h('div', { class: 'panel h-96 animate-pulse-breath' })
        )
      );
      return;
    }

    const status = describeEpisodeStatus(episode, {
      cropConfig: episode.crop_config,
      clips: state.clips,
    });
    const selected = state.clips.filter(
      (clip) => clip.review.selection.status === 'selected'
    );
    const approved = selected.filter((clip) =>
      clipDistributionReady(clip.review)
    );
    const pending = selected.filter(
      (clip) => !clipDistributionReady(clip.review)
    );
    const unselected = state.clips.filter(
      (clip) => clip.review.selection.status === 'unselected'
    );
    const rejected = state.clips.filter(
      (clip) => clip.review.selection.status === 'rejected'
    );

    page.replaceChildren(
      renderHeader(episodeId, episode, status),
      h(
        'div',
        {
          class:
            'max-w-[1200px] mx-auto w-full px-8 py-6 flex flex-col gap-6 pb-32',
        },
        renderOverview(approved.length, pending.length, rejected.length, episode),
        renderPlatforms(state.enabled_destinations, selected),
        renderClipList(approved, pending, unselected, rejected)
      ),
      renderPublishBar(
        episodeId,
        status,
        episode.quality as QualitySnapshot | null | undefined,
        approved.length,
        publishing(),
        publishing
      )
    );
  });

  mount(target, page);
}

function renderHeader(
  episodeId: string,
  episode: UnknownRecord,
  status: StatusDescriptor
): HTMLElement {
  return h(
    'header',
    {
      class:
        'sticky top-0 z-10 bg-canvas border-b border-border-subtle px-8 py-4 flex items-center gap-5',
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
          { class: 'text-heading-sm uppercase text-ink-tertiary' },
          'Publish'
        ),
        StatusPill({ descriptor: status, size: 'sm' })
      ),
      h(
        'div',
        { class: 'text-body-lg text-ink-primary font-medium mt-1 truncate' },
        episodeTitle(episode, episodeId)
      )
    )
  );
}

function renderOverview(
  approved: number,
  pending: number,
  rejected: number,
  episode: UnknownRecord
): HTMLElement {
  const longformUrl = String(episode.youtube_longform_url ?? '');
  return h(
    'div',
    { class: 'panel p-6 flex items-center gap-10 flex-wrap' },
    statTile('Distribution ready', String(approved), 'status-success'),
    statTile('Selected · not ready', String(pending), 'ink-primary'),
    statTile('Rejected · deferred', String(rejected), 'ink-secondary'),
    statTile(
      'Longform',
      longformUrl ? 'YouTube uploaded' : 'Not uploaded',
      longformUrl ? 'status-success' : 'ink-secondary'
    ),
    statTile(
      'Duration',
      formatDuration(episodeDisplayDuration(episode)),
      'ink-primary'
    )
  );
}

function statTile(label: string, value: string, tone: string): HTMLElement {
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
        class: `text-display-md font-display text-${tone} font-mono tabular`,
      },
      value
    )
  );
}

function renderPlatforms(
  destinations: ReviewDestination[],
  selected: ReviewedClip[]
): HTMLElement {
  return h(
    'div',
    { class: 'panel p-5' },
    h(
      'div',
      { class: 'text-heading-sm uppercase text-ink-tertiary mb-4' },
      'Enabled destination copy'
    ),
    h(
      'div',
      { class: 'grid grid-cols-2 lg:grid-cols-4 gap-3' },
      ...destinations.map((destination) => {
        const completeCount = selected.filter((clip) =>
          clip.review.metadata.destinations.some(
            (item) => item.key === destination.key && item.complete
          )
        ).length;
        const complete = selected.length > 0 && completeCount === selected.length;
        return h(
          'div',
          {
            class:
              'flex items-center gap-3 px-3 py-2.5 rounded-md bg-surface-2 border border-border-subtle',
          },
          h('span', {
            class: 'w-2 h-2 rounded-full',
            style: {
              background:
                DESTINATION_COLORS[destination.key] ?? 'var(--ink-tertiary)',
            },
          }),
          h(
            'span',
            { class: 'flex-1 text-body text-ink-primary font-medium' },
            destination.label
          ),
          h(
            'span',
            {
              class: [
                'text-code font-mono tabular',
                complete ? 'text-status-success' : 'text-ink-tertiary',
              ].join(' '),
            },
            `${completeCount}/${selected.length}`
          )
        );
      })
    )
  );
}

function renderClipList(
  approved: ReviewedClip[],
  pending: ReviewedClip[],
  unselected: ReviewedClip[],
  rejected: ReviewedClip[]
): HTMLElement {
  const rows = [
    ...approved.map((clip) => ({
      clip,
      state: 'Ready for distribution',
      tone: 'success',
    })),
    ...pending.map((clip) => ({
      clip,
      state: 'Version needs approval',
      tone: 'warning',
    })),
    ...unselected.map((clip) => ({ clip, state: 'Not selected', tone: 'neutral' })),
    ...rejected.map((clip) => ({ clip, state: 'Rejected · deferred', tone: 'danger' })),
  ];
  return h(
    'div',
    { class: 'panel p-5 flex flex-col gap-3' },
    h(
      'div',
      { class: 'text-heading-sm uppercase text-ink-tertiary' },
      `Clips · ${pluralize(rows.length, 'candidate')}`
    ),
    pending.length > 0
      ? h(
          'p',
          { class: 'text-body text-status-warning' },
          `${pluralize(pending.length, 'selected clip')} still ${
            pending.length === 1 ? 'needs' : 'need'
          } a current approval for its chosen distribution version.`
        )
      : null,
    h(
      'ul',
      { class: 'flex flex-col divide-y divide-border-subtle' },
      ...rows.map(({ clip, state, tone }) =>
        h(
          'li',
          { class: 'flex items-center gap-3 py-2.5' },
          h('span', {
            class: [
              'w-2 h-2 rounded-full',
              tone === 'success'
                ? 'bg-status-success'
                : tone === 'warning'
                  ? 'bg-status-warning'
                  : tone === 'danger'
                    ? 'bg-status-danger'
                    : 'bg-ink-tertiary',
            ].join(' '),
          }),
          h(
            'span',
            { class: 'text-body text-ink-primary flex-1 truncate' },
            String(clip.title || 'Untitled clip')
          ),
          clip.review.selection.status === 'selected'
            ? h(
                'span',
                { class: 'chip text-ink-primary' },
                clipDistributionLabel(clip.review)
              )
            : null,
          h(
            'span',
            { class: 'text-body-sm text-ink-tertiary' },
            state
          ),
          h(
            'span',
            { class: 'text-code-sm text-ink-tertiary font-mono tabular' },
            formatDuration(clipDuration(clip))
          )
        )
      )
    )
  );
}

function clipDuration(clip: ReviewedClip): number {
  if (typeof clip.duration === 'number') return clip.duration;
  return Number(clip.end_seconds ?? 0) - Number(clip.start_seconds ?? 0);
}

function renderPublishBar(
  episodeId: string,
  status: StatusDescriptor,
  quality: QualitySnapshot | null | undefined,
  approvedCount: number,
  publishing: boolean,
  publishingSignal: Signal<boolean>
): HTMLElement {
  const canPublish =
    quality?.release_gate.status === 'awaiting_publish_approval' &&
    quality.release_gate.can_approve_publish &&
    approvedCount > 0;

  return h(
    'footer',
    {
      class:
        'sticky bottom-0 z-20 border-t border-border-subtle bg-canvas/95 backdrop-blur-md px-8 py-4',
    },
    h(
      'div',
      { class: 'max-w-[1200px] mx-auto flex items-center gap-4' },
      h(
        'div',
        { class: 'flex-1' },
        h(
          'div',
          { class: 'text-body text-ink-primary font-medium' },
          canPublish
            ? `${pluralize(approvedCount, 'distribution-ready clip')} ready`
            : status.label
        ),
        h(
          'div',
          { class: 'text-body-sm text-ink-secondary' },
          canPublish
            ? 'Explicit approval starts uploads to the enabled destinations.'
            : status.hint
        )
      ),
      canPublish
        ? Button({
            variant: 'primary',
            size: 'lg',
            label: publishing ? 'Starting publish…' : 'Publish everywhere',
            loading: publishing,
            disabled: publishing,
            onClick: async () => {
              publishingSignal.set(true);
              try {
                await api.approvePublish(episodeId);
                showToast('Publishing started.', 'success');
                navigate(`/episodes/${episodeId}`);
              } catch (error) {
                showToast((error as Error).message, 'error');
              } finally {
                publishingSignal.set(false);
              }
            },
          })
        : null
    )
  );
}
