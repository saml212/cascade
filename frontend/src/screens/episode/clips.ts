import { h } from '../../lib/dom';
import { Button } from '../../components/Button';
import { navigate } from '../../lib/router';
import { pluralize } from '../../lib/format';

export function renderClips(
  target: HTMLElement,
  ep: Record<string, unknown>,
  episodeId: string
): void {
  const clips = (ep.clips as unknown[]) ?? [];
  target.replaceChildren(
    h(
      'div',
      { class: 'panel p-10 flex items-center justify-between' },
      h(
        'div',
        null,
        h(
          'div',
          { class: 'font-display text-display-md text-ink-primary' },
          clips.length > 0
            ? `${pluralize(clips.length, 'clip')} ready for review.`
            : 'Clips haven’t been mined yet.'
        ),
        h(
          'p',
          { class: 'text-body text-ink-secondary mt-2 max-w-xl' },
          clips.length > 0
            ? 'Open the editorial review surface to keep, reject, retitle, or trim each clip, and sign off per-platform metadata.'
            : 'The clip miner runs after longform approval. Once it finishes, clips will appear here for review.'
        )
      ),
      clips.length > 0
        ? Button({
            variant: 'primary',
            size: 'lg',
            label: 'Review clips',
            onClick: () => navigate(`/episodes/${episodeId}/clips/review`),
          })
        : null
    )
  );
}
