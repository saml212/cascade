import { h } from '../../lib/dom';
import { describeStatus, formatDuration, formatRelative } from '../../lib/format';
import { Button } from '../../components/Button';
import { Icon } from '../../components/icons';
import { api, type DeliveryStatus } from '../../lib/api';
import { navigate } from '../../lib/router';
import { showToast } from '../../state/ui';

export function renderLongform(
  target: HTMLElement,
  ep: Record<string, unknown>,
  episodeId: string
): void {
  const status = describeStatus(ep.status as string);
  const longformReady =
    status.key === 'awaiting_longform_review' ||
    status.key === 'awaiting_clip_review' ||
    status.key === 'awaiting_publish' ||
    status.key === 'awaiting_backup' ||
    status.key === 'live';

  const delivery = ep.delivery as DeliveryStatus | undefined;
  const deliveryReady =
    delivery?.video_status === 'ready' && !!delivery.video_download_url;
  const hasPlayableVideo = deliveryReady || longformReady;
  const videoUrl = deliveryReady
    ? delivery.video_download_url!
    : `/media/episodes/${episodeId}/longform.mp4`;
  const duration = deliveryReady
    ? delivery.video?.duration_seconds ?? delivery.duration_seconds ?? null
    : (ep.duration_seconds as number) ?? null;
  const youtubeUrl = (ep.youtube_longform_url as string) ?? '';
  const spotifyUrl = (ep.spotify_longform_url as string) ?? '';
  const pipeline = ep.pipeline as Record<string, unknown> | undefined;
  const renderedAt =
    (deliveryReady ? delivery.video_completed_at : null) ??
    (ep?.longform_rendered_at as string) ??
    (pipeline?.completed_at as string) ??
    '';

  target.replaceChildren(
    h(
      'div',
      { class: 'grid grid-cols-1 xl:grid-cols-[2fr_1fr] gap-6' },
      // Player column
      hasPlayableVideo
        ? h(
            'div',
            { class: 'panel overflow-hidden' },
            !deliveryReady
              ? h(
                  'div',
                  {
                    class:
                      'px-4 py-2 bg-status-warning/10 border-b border-status-warning/30 text-body-sm text-status-warning',
                  },
                  'Earlier render — prepare the verified upload video before release.'
                )
              : null,
            h('video', {
              src: videoUrl,
              poster: `/api/episodes/${episodeId}/crop-frame`,
              controls: true,
              preload: 'metadata',
              class: 'w-full bg-black block',
              style: { maxHeight: '64vh' },
            })
          )
        : h(
            'div',
            {
              class: 'panel p-16 text-center',
            },
            h(
              'div',
              {
                class:
                  'font-display text-display-md text-ink-secondary mb-2',
              },
              'No longform yet.'
            ),
            h(
              'p',
              { class: 'text-body text-ink-tertiary max-w-md mx-auto' },
              'Cascade renders the longform after crop setup and audio sync. You’ll see the player here when it’s ready.'
            )
          ),
      // Info column
      h(
        'div',
        { class: 'flex flex-col gap-4' },
        hasPlayableVideo
          ? h(
              'div',
              { class: 'panel p-5 flex flex-col gap-3' },
              h(
                'div',
                {
                  class: 'text-heading-sm uppercase text-ink-tertiary',
                },
                deliveryReady ? 'Verified upload video' : 'Earlier render'
              ),
              detailRow('Duration', formatDuration(duration)),
              renderedAt
                ? detailRow('Rendered', formatRelative(renderedAt))
                : null,
              deliveryReady
                ? Button({
                    variant: 'primary',
                    size: 'md',
                    label: 'Open upload files',
                    icon: Icon.chevronRight(),
                    onClick: () => navigate(`/episodes/${episodeId}/delivery`),
                    class: 'w-full',
                  })
                : Button({
                    variant: 'primary',
                    size: 'md',
                    label: 'Prepare for upload',
                    icon: Icon.chevronRight(),
                    onClick: () => navigate(`/episodes/${episodeId}/delivery`),
                    class: 'w-full',
                  }),
              longformReady
                ? Button({
                    variant: 'secondary',
                    size: 'md',
                    label:
                      status.key === 'awaiting_longform_review'
                        ? 'Watch & review edit'
                        : 'Open edit review',
                    onClick: () =>
                      navigate(`/episodes/${episodeId}/longform/review`),
                    class: 'w-full',
                  })
                : null,
              status.key === 'awaiting_longform_review'
                ? Button({
                    variant: 'secondary',
                    size: 'md',
                    label: 'Approve without watching',
                    onClick: async () => {
                      try {
                        await api.approveLongform(episodeId);
                        showToast(
                          'Longform approved — clip mining begins.',
                          'success'
                        );
                        navigate(`/episodes/${episodeId}`);
                      } catch (e) {
                        showToast((e as Error).message, 'error');
                      }
                    },
                    class: 'w-full',
                  })
                : null
            )
          : null,
        // Platform URLs / linkout surface
        youtubeUrl || spotifyUrl
          ? h(
              'div',
              { class: 'panel p-5 flex flex-col gap-3' },
              h(
                'div',
                {
                  class: 'text-heading-sm uppercase text-ink-tertiary',
                },
                'Live on'
              ),
              youtubeUrl ? externalLink('YouTube', youtubeUrl) : null,
              spotifyUrl ? externalLink('Spotify', spotifyUrl) : null
            )
          : hasPlayableVideo
          ? h(
              'div',
              {
                class: 'panel p-5',
              },
              h(
                'div',
                {
                  class: 'text-heading-sm uppercase text-ink-tertiary mb-2',
                },
                'Not published yet'
              ),
              h(
                'p',
                { class: 'text-body-sm text-ink-secondary leading-relaxed' },
                'Publishing links appear here after each platform confirms the upload.'
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
    h(
      'span',
      { class: 'flex items-center gap-2' },
      label
    ),
    h(
      'span',
      { class: 'text-ink-tertiary' },
      '↗'
    )
  );
}
