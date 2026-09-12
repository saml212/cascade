import { h } from '../lib/dom';
import { link } from '../lib/router';
import { Icon } from './icons';

/** Shared semantic navigation control for episode detail tools. */
export function EpisodeBackButton(
  episodeId: string,
  className = ''
): HTMLAnchorElement {
  return h(
    'a',
    {
      ...link(`/episodes/${episodeId}`),
      class: `w-8 h-8 flex items-center justify-center rounded-md text-ink-tertiary hover:text-ink-primary hover:bg-surface-2 ${className}`,
      title: 'Back to episode',
      'aria-label': 'Back to episode',
    },
    Icon.chevronLeft()
  ) as HTMLAnchorElement;
}
