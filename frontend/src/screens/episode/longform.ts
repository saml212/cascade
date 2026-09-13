import { Button } from '../../components/Button';
import { Icon } from '../../components/icons';
import { QualityReview } from '../../components/QualityReview';
import { h } from '../../lib/dom';
import {
  api,
  type EpisodeReviewState,
  type QualitySnapshot,
} from '../../lib/api';
import { describeStatus, formatDuration, formatRelative } from '../../lib/format';
import { currentPath, navigate } from '../../lib/router';
import { showToast } from '../../state/ui';

export function renderLongform(
  target: HTMLElement,
  ep: Record<string, unknown>,
  episodeId: string
): void {
  target.replaceChildren(
    h('div', { class: 'panel p-12 text-center text-ink-tertiary' }, 'Loading review state…')
  );
  void api
    .review(episodeId)
    .then((review) => {
      if (currentPath() === `/episodes/${episodeId}/longform`) {
        renderLongformState(target, ep, episodeId, review);
      }
    })
    .catch((error: Error) => {
      if (currentPath() === `/episodes/${episodeId}/longform`) {
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

function renderLongformState(
  target: HTMLElement,
  ep: Record<string, unknown>,
  episodeId: string,
  review: EpisodeReviewState
): void {
  const status = describeStatus(ep.status as string);
  const artifact = review.longform.render;
  const playable = artifact.playable && Boolean(artifact.url);
  const current = artifact.current;
  const approved = review.longform.approval.current;
  const quality = ep.quality as QualitySnapshot | null | undefined;
  const recordedDuration =
    (artifact.output_duration_seconds as number | undefined) ?? null;
  const youtubeUrl = (ep.youtube_longform_url as string) ?? '';
  const spotifyUrl = (ep.spotify_longform_url as string) ?? '';

  const durationRow = playable
    ? detailRow(
        'Duration',
        recordedDuration == null ? 'Reading media…' : formatDuration(recordedDuration)
      )
    : null;
  const player = playable
    ? (h('video', {
        src: artifact.url!,
        poster: `/api/episodes/${episodeId}/crop-frame`,
        controls: true,
        preload: 'metadata',
        class: 'w-full bg-black block',
        style: { maxHeight: '64vh' },
        'aria-label': 'Longform rendered video review',
      }) as HTMLVideoElement)
    : null;
  if (player && durationRow && recordedDuration == null) {
    player.addEventListener('loadedmetadata', () => {
      const value = durationRow.lastElementChild as HTMLElement | null;
      if (value && Number.isFinite(player.duration)) {
        value.textContent = formatDuration(player.duration);
      }
    });
  }

  target.replaceChildren(
    h(
      'div',
      { class: 'grid grid-cols-1 xl:grid-cols-[2fr_1fr] gap-6' },
      playable
        ? h(
            'div',
            { class: 'panel overflow-hidden' },
            !current
              ? h(
                  'div',
                  {
                    class:
                      'px-4 py-3 bg-status-warning/10 border-b border-status-warning/30 text-body-sm text-status-warning',
                    role: 'status',
                  },
                  'Previous render · current speaker-cut version required before approval',
                  h('span', { class: 'block text-ink-secondary mt-1' }, artifact.detail)
                )
              : null,
            player
          )
        : h(
            'div',
            { class: 'panel p-16 text-center' },
            h(
              'div',
              { class: 'font-display text-display-md text-ink-secondary mb-2' },
              'No rendered longform yet.'
            ),
            h(
              'p',
              { class: 'text-body text-ink-tertiary max-w-md mx-auto' },
              'Open edit review to inspect the source-clock cut, then prepare the speaker-cut video.'
            )
          ),
      h(
        'div',
        { class: 'flex flex-col gap-4' },
        QualityReview({ episodeId, quality, compact: true }),
        h(
          'div',
          { class: 'panel p-5 flex flex-col gap-3' },
          h(
            'div',
            { class: 'text-heading-sm uppercase text-ink-tertiary' },
            current
              ? approved
                ? 'Current approved render'
                : 'Current render · review required'
              : playable
                ? 'Previous review render'
                : 'Render required'
          ),
          durationRow,
          artifact.completed_at
            ? detailRow('Rendered', formatRelative(artifact.completed_at))
            : null,
          Button({
            variant: 'primary',
            size: 'md',
            label: 'Open source edit review',
            onClick: () => navigate(`/episodes/${episodeId}/longform/review`),
            class: 'w-full',
          }),
          Button({
            variant: 'secondary',
            size: 'md',
            label: playable ? 'Open upload files' : 'Prepare for upload',
            icon: Icon.chevronRight(),
            onClick: () => navigate(`/episodes/${episodeId}/delivery`),
            class: 'w-full',
          }),
          artifact.download_url
            ? h(
                'a',
                {
                  href: artifact.download_url,
                  download: artifact.path.split('/').pop() ?? 'longform.mp4',
                  class:
                    'text-body-sm text-center text-ink-secondary hover:text-ink-primary underline underline-offset-4',
                },
                'Download this render'
              )
            : null,
          current && !approved && status.key === 'awaiting_longform_review'
            ? Button({
                variant: 'secondary',
                size: 'md',
                label: 'Approve current render',
                onClick: async () => {
                  try {
                    const continueProduction = review.clip_summary.candidate_count === 0;
                    await api.approveLongform(episodeId, {
                      continue_production: continueProduction,
                    });
                    showToast(
                      continueProduction
                        ? 'Current longform approved. Local clip production started.'
                        : 'Current longform approved. Existing clips were preserved.',
                      'success'
                    );
                    navigate(`/episodes/${episodeId}`);
                  } catch (error) {
                    showToast((error as Error).message, 'error');
                  }
                },
                class: 'w-full',
              })
            : null
        ),
        youtubeUrl || spotifyUrl
          ? h(
              'div',
              { class: 'panel p-5 flex flex-col gap-3' },
              h(
                'div',
                { class: 'text-heading-sm uppercase text-ink-tertiary' },
                'Live on'
              ),
              youtubeUrl ? externalLink('YouTube', youtubeUrl) : null,
              spotifyUrl ? externalLink('Spotify', spotifyUrl) : null
            )
          : playable
            ? h(
                'div',
                { class: 'panel p-5' },
                h(
                  'div',
                  { class: 'text-heading-sm uppercase text-ink-tertiary mb-2' },
                  'Not published yet'
                ),
                h(
                  'p',
                  { class: 'text-body-sm text-ink-secondary leading-relaxed' },
                  'Publishing links appear after each platform confirms the upload.'
                )
              )
            : null
      )
    )
  );
}

function detailRow(label: string, value: string): HTMLElement {
  return h(
    'div',
    {
      class:
        'flex items-baseline justify-between py-2 border-b border-border-subtle last:border-0',
    },
    h('span', { class: 'text-body-sm text-ink-tertiary' }, label),
    h(
      'span',
      { class: 'text-body text-ink-primary font-mono tabular' },
      value
    )
  );
}

function externalLink(label: string, url: string): HTMLElement {
  return h(
    'a',
    {
      href: url,
      target: '_blank',
      rel: 'noopener noreferrer',
      class:
        'flex items-center justify-between px-3 py-2 rounded-md bg-surface-2 border border-border-subtle hover:border-border-strong text-body text-ink-primary transition-colors duration-[120ms]',
    },
    h('span', { class: 'flex items-center gap-2' }, label),
    h('span', { class: 'text-ink-tertiary' }, '↗')
  );
}
