import { h, mount } from '../lib/dom';
import { effect } from '../lib/signals';
import { episodes, startEpisodesPoll } from '../state/episodes';
import { type EpisodeSummary } from '../lib/api';
import {
  describeStatus,
  episodeDateLabel,
  formatDuration,
  formatRelative,
  pluralize,
} from '../lib/format';
import { StatusPill } from '../components/StatusPill';
import { Button } from '../components/Button';
import { Icon } from '../components/icons';
import { link, navigate } from '../lib/router';

const PRIORITY: Record<string, number> = {
  awaiting_crop: 100,
  awaiting_longform_review: 90,
  awaiting_clip_review: 85,
  awaiting_publish: 80,
  awaiting_backup: 70,
  processing: 60,
  error: 55,
  queued: 40,
  live: 10,
  cancelled: 5,
};

function pickSpotlight(list: EpisodeSummary[]): EpisodeSummary | null {
  if (list.length === 0) return null;
  const scored = list.map((ep) => {
    const key = describeStatus(ep.status).key;
    return { ep, score: PRIORITY[key] ?? 0 };
  });
  scored.sort((a, b) => b.score - a.score);
  return scored[0].ep;
}

function titleOf(ep: EpisodeSummary): string {
  const g = ep.guest_name?.trim();
  if (g) return g;
  if (ep.episode_name?.trim()) return ep.episode_name!;
  if (ep.title?.trim()) return ep.title!;
  return `Untitled — ${episodeDateLabel(ep.episode_id)}`;
}

function subtitleOf(ep: EpisodeSummary): string {
  const parts: string[] = [];
  if (ep.guest_title) parts.push(ep.guest_title);
  if (ep.episode_name && ep.guest_name) parts.push(ep.episode_name);
  return parts.join(' · ');
}

function clipCountOf(ep: EpisodeSummary): number {
  return Array.isArray(ep.clips) ? ep.clips.length : 0;
}

export function Dashboard(target: HTMLElement): void {
  startEpisodesPoll();

  const hero = h('section', { class: 'mb-10' });
  const table = h('section');
  const count = h('span', { class: 'text-body-sm text-ink-tertiary' }, '');

  effect(() => {
    const list = episodes();
    count.textContent = list ? pluralize(list.length, 'episode') : '';
  });

  effect(() => {
    const list = episodes();

    if (list == null) {
      hero.replaceChildren(
        h(
          'div',
          { class: 'panel h-40 animate-pulse-breath' },
          h('div', { class: 'px-8 py-6 text-ink-tertiary' }, 'Loading episodes…')
        )
      );
      table.replaceChildren();
      return;
    }

    if (list.length === 0) {
      hero.replaceChildren(emptyHero());
      table.replaceChildren();
      return;
    }

    const spotlight = pickSpotlight(list);
    hero.replaceChildren(spotlight ? heroSpotlight(spotlight) : emptyHero());
    table.replaceChildren(episodesTable(list));
  });

  mount(
    target,
    h(
      'div',
      { class: 'max-w-[1280px] mx-auto px-10 py-10' },
      h(
        'header',
        { class: 'flex items-baseline justify-between mb-10' },
        h(
          'div',
          null,
          h(
            'h1',
            { class: 'font-display text-display-xl leading-none' },
            'Today’s episode'
          ),
          h(
            'p',
            { class: 'text-body text-ink-secondary mt-3' },
            'What’s on deck, what needs you, what’s already live.'
          )
        ),
        Button({
          variant: 'primary',
          size: 'lg',
          label: 'New episode',
          icon: Icon.plus(),
          onClick: () => navigate('/new'),
        })
      ),
      hero,
      h(
        'div',
        { class: 'flex items-baseline justify-between mb-4' },
        h('h2', { class: 'text-heading-sm uppercase text-ink-tertiary' }, 'All episodes'),
        count
      ),
      table
    )
  );
}

function emptyHero(): HTMLElement {
  return h(
    'div',
    {
      class:
        'panel p-10 flex flex-col items-start gap-4',
    },
    h(
      'div',
      {
        class: 'font-display text-display-lg text-ink-primary',
      },
      'Nothing on deck.'
    ),
    h(
      'p',
      { class: 'text-body-lg text-ink-secondary max-w-lg leading-relaxed' },
      'Plug in an SD card from the camera or the H6E recorder, then start a new episode to kick off the pipeline.'
    ),
    Button({
      variant: 'primary',
      label: 'New episode',
      icon: Icon.plus(),
      onClick: () => navigate('/new'),
    })
  );
}

function heroSpotlight(ep: EpisodeSummary): HTMLElement {
  const status = describeStatus(ep.status);
  const cta = ctaFor(ep);

  return h(
    'a',
    {
      ...link(`/episodes/${ep.episode_id}`),
      class:
        'block panel p-8 hover:border-border-strong transition-colors duration-[120ms] group',
    },
    h(
      'div',
      { class: 'flex gap-10 items-start' },
      h(
        'div',
        { class: 'flex-1 min-w-0' },
        h(
          'div',
          { class: 'flex items-center gap-3 mb-4' },
          StatusPill({ descriptor: status, size: 'md' }),
          h(
            'span',
            { class: 'text-code text-ink-tertiary font-mono tabular' },
            ep.episode_id
          )
        ),
        h(
          'h2',
          {
            class:
              'font-display text-display-xl leading-tight text-ink-primary group-hover:text-accent transition-colors duration-[120ms]',
          },
          titleOf(ep)
        ),
        subtitleOf(ep)
          ? h(
              'p',
              { class: 'text-body-lg text-ink-secondary mt-2' },
              subtitleOf(ep)
            )
          : null,
        h(
          'div',
          { class: 'flex items-center gap-6 mt-6 text-body-sm text-ink-secondary' },
          meta('Duration', formatDuration(ep.duration_seconds)),
          meta('Created', formatRelative(ep.created_at)),
          meta('Clips', clipCountOf(ep) > 0 ? String(clipCountOf(ep)) : '—')
        ),
        status.hint
          ? h(
              'p',
              {
                class:
                  'text-body-lg text-ink-primary mt-6 max-w-xl leading-relaxed',
              },
              status.hint
            )
          : null
      ),
      h(
        'div',
        { class: 'shrink-0 flex flex-col items-end gap-3' },
        h(
          'div',
          { class: 'text-heading-sm uppercase text-ink-tertiary' },
          'Next step'
        ),
        h(
          'div',
          { class: 'font-display text-display-md text-accent' },
          cta.label
        )
      )
    )
  );
}

function meta(label: string, value: string): HTMLElement {
  return h(
    'div',
    { class: 'flex items-baseline gap-2' },
    h('span', { class: 'text-ink-tertiary' }, label),
    h('span', { class: 'text-ink-primary font-mono tabular' }, value)
  );
}

function ctaFor(ep: EpisodeSummary): { label: string } {
  const key = describeStatus(ep.status).key;
  switch (key) {
    case 'awaiting_crop':
      return { label: 'Set up crops →' };
    case 'awaiting_longform_review':
      return { label: 'Review longform →' };
    case 'awaiting_clip_review':
      return { label: 'Review clips →' };
    case 'awaiting_publish':
      return { label: 'Publish →' };
    case 'awaiting_backup':
      return { label: 'Approve backup →' };
    case 'processing':
      return { label: 'Watch progress →' };
    case 'live':
      return { label: 'See results →' };
    case 'error':
      return { label: 'Resolve error →' };
    default:
      return { label: 'Open episode →' };
  }
}

function episodesTable(list: EpisodeSummary[]): HTMLElement {
  const rows = [...list].sort(
    (a, b) =>
      new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
  );

  return h(
    'div',
    { class: 'panel overflow-hidden' },
    h(
      'div',
      {
        class:
          'grid grid-cols-[1fr_160px_140px_110px_80px] gap-4 px-5 py-3 text-heading-sm uppercase text-ink-tertiary border-b border-border-subtle',
      },
      h('div', null, 'Episode'),
      h('div', null, 'Status'),
      h('div', { class: 'text-right' }, 'Duration'),
      h('div', { class: 'text-right' }, 'Clips'),
      h('div', { class: 'text-right' }, 'Created')
    ),
    ...rows.map((ep) => {
      const status = describeStatus(ep.status);
      return h(
        'a',
        {
          ...link(`/episodes/${ep.episode_id}`),
          class:
            'grid grid-cols-[1fr_160px_140px_110px_80px] gap-4 px-5 py-4 border-b border-border-subtle last:border-0 hover:bg-surface-2 transition-colors duration-[120ms] items-center',
        },
        h(
          'div',
          { class: 'min-w-0' },
          h(
            'div',
            {
              class:
                'text-body-lg text-ink-primary font-medium truncate',
            },
            titleOf(ep)
          ),
          h(
            'div',
            {
              class:
                'text-code-sm text-ink-tertiary font-mono tabular truncate mt-0.5',
            },
            ep.episode_id
          )
        ),
        StatusPill({ descriptor: status, size: 'sm' }),
        h(
          'div',
          {
            class: 'text-body text-ink-primary font-mono tabular text-right',
          },
          formatDuration(ep.duration_seconds)
        ),
        h(
          'div',
          {
            class: 'text-body text-ink-secondary font-mono tabular text-right',
          },
          clipCountOf(ep) > 0 ? String(clipCountOf(ep)) : '—'
        ),
        h(
          'div',
          {
            class: 'text-body-sm text-ink-tertiary text-right',
          },
          formatRelative(ep.created_at)
        )
      );
    })
  );
}
