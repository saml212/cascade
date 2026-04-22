import { h, mount } from '../lib/dom';
import { Icon } from '../components/icons';

const UPCOMING = [
  {
    title: 'Per-clip performance',
    detail:
      'YouTube views, TikTok watch time, cross-platform share-of-voice by virality score bucket.',
  },
  {
    title: 'Episode half-life',
    detail:
      'How long each episode’s longform keeps earning impressions versus how long it stays on the schedule.',
  },
  {
    title: 'Hook-line correlation',
    detail:
      'Which opening phrases actually pull — by platform and by guest type — so future clips can be mined for them.',
  },
  {
    title: 'Platform comparison',
    detail:
      'Side-by-side retention graphs so Sam can tell which platform is worth the most editorial effort next week.',
  },
];

export function Analytics(target: HTMLElement): void {
  mount(
    target,
    h(
      'div',
      { class: 'max-w-[1100px] mx-auto px-10 py-16' },
      h(
        'header',
        { class: 'mb-12 max-w-2xl' },
        h(
          'h1',
          { class: 'font-display text-display-xl text-ink-primary mb-4' },
          'Too early to tell.'
        ),
        h(
          'p',
          { class: 'text-body-lg text-ink-secondary leading-relaxed' },
          'Performance data lands a week after each episode publishes. Until then, here’s what cascade will report when the numbers arrive.'
        )
      ),
      h(
        'div',
        {
          class:
            'grid grid-cols-2 gap-4',
        },
        ...UPCOMING.map((item) =>
          h(
            'div',
            {
              class:
                'panel p-6 flex flex-col gap-2 opacity-70 border-border-subtle',
            },
            h(
              'div',
              { class: 'flex items-center gap-2' },
              h(
                'span',
                {
                  class:
                    'text-code-sm uppercase tracking-wide text-ink-tertiary font-mono',
                },
                'Coming'
              ),
              h('span', {
                class: 'w-1 h-1 rounded-full bg-ink-tertiary',
              })
            ),
            h(
              'h3',
              { class: 'text-heading-md text-ink-primary' },
              item.title
            ),
            h(
              'p',
              { class: 'text-body-sm text-ink-secondary leading-relaxed' },
              item.detail
            )
          )
        )
      ),
      h(
        'div',
        {
          class:
            'mt-8 panel p-5 flex items-center gap-3 text-body-sm text-ink-tertiary',
        },
        Icon.chart({ size: 16 }),
        h(
          'span',
          null,
          'This page unlocks once the first published episode has a week of data.'
        )
      )
    )
  );
}
