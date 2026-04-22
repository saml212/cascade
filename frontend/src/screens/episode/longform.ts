import { h } from '../../lib/dom';
import { describeStatus } from '../../lib/format';
import { Button } from '../../components/Button';
import { api } from '../../lib/api';
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
    status.key === 'live';

  const videoUrl = `/media/episodes/${episodeId}/longform.mp4`;

  target.replaceChildren(
    h(
      'div',
      { class: 'flex flex-col gap-6' },
      longformReady
        ? h(
            'div',
            { class: 'panel overflow-hidden' },
            h('video', {
              src: videoUrl,
              controls: true,
              class: 'w-full bg-black',
              style: { maxHeight: '68vh' },
            })
          )
        : h(
            'div',
            {
              class:
                'panel p-16 text-center',
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
      longformReady && status.key === 'awaiting_longform_review'
        ? h(
            'div',
            { class: 'panel p-6 flex items-center gap-4 justify-between' },
            h(
              'div',
              null,
              h(
                'div',
                { class: 'text-heading-md text-ink-primary' },
                'Ready to approve?'
              ),
              h(
                'p',
                { class: 'text-body text-ink-secondary mt-1' },
                'Approving kicks off YouTube upload, the RSS update, and clip mining.'
              )
            ),
            h(
              'div',
              { class: 'flex gap-2' },
              Button({
                variant: 'secondary',
                label: 'Request edits',
                onClick: () => {
                  showToast('Editing surface is on the way — coming next.');
                },
              }),
              Button({
                variant: 'primary',
                label: 'Approve longform',
                onClick: async () => {
                  try {
                    await api.approveLongform(episodeId);
                    showToast('Longform approved — running clip miner, shorts, metadata, and thumbnails.', 'success');
                    navigate(`/episodes/${episodeId}`);
                  } catch (e) {
                    showToast((e as Error).message, 'error');
                  }
                },
              })
            )
          )
        : null
    )
  );
}
