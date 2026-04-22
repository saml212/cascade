import { h, mount } from '../lib/dom';
import { effect } from '../lib/signals';
import { episodeDetail, watchEpisode } from '../state/episodes';
import { agentPanelCollapsed } from '../state/ui';
import { Icon } from '../components/icons';
import { link } from '../lib/router';

export function CropSetup(target: HTMLElement, episodeId: string): void {
  watchEpisode(episodeId);
  // Auto-collapse agent panel on entry so we can use full width.
  agentPanelCollapsed.set(true);

  const content = h('div', { class: 'flex-1 min-h-0' });

  effect(() => {
    const ep = episodeDetail();
    if (!ep) {
      content.replaceChildren(
        h(
          'div',
          {
            class: 'flex-1 grid place-items-center text-ink-tertiary',
          },
          'Loading episode…'
        )
      );
      return;
    }
    content.replaceChildren(placeholder(episodeId));
  });

  mount(
    target,
    h(
      'div',
      { class: 'min-h-full flex flex-col' },
      h(
        'header',
        {
          class:
            'flex items-center gap-4 px-8 py-5 border-b border-border-subtle',
        },
        h(
          'a',
          {
            ...link(`/episodes/${episodeId}`),
            class:
              'w-8 h-8 flex items-center justify-center text-ink-tertiary hover:text-ink-primary rounded-md hover:bg-surface-2',
          },
          Icon.chevronLeft()
        ),
        h(
          'div',
          null,
          h(
            'div',
            { class: 'font-display text-display-md text-ink-primary' },
            'Crop setup'
          ),
          h(
            'div',
            { class: 'text-body-sm text-ink-tertiary font-mono tabular' },
            episodeId
          )
        )
      ),
      content
    )
  );
}

function placeholder(episodeId: string): HTMLElement {
  return h(
    'div',
    { class: 'p-10 max-w-[1200px] mx-auto w-full' },
    h(
      'div',
      {
        class: 'panel p-10',
      },
      h(
        'div',
        {
          class: 'font-display text-display-md text-ink-primary mb-3',
        },
        'Crop setup lands next.'
      ),
      h(
        'p',
        { class: 'text-body-lg text-ink-secondary max-w-2xl mb-4 leading-relaxed' },
        'This screen takes over the full window and combines the video scrubber, speaker crop canvas, sync verification, and audio mixer into one editing surface. The design-system doc has the layout. Implementation follows right after the skeleton pass.'
      ),
      h(
        'p',
        { class: 'text-body-sm text-ink-tertiary font-mono tabular' },
        `episode_id: ${episodeId}`
      )
    )
  );
}
