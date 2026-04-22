/**
 * Schedule — seven-day publish calendar. Pulls /api/schedule and lays
 * clips into per-day columns with a platform-colored pill per entry.
 */

import { h, mount } from '../lib/dom';
import { signal, effect } from '../lib/signals';
import { api, type UnknownRecord } from '../lib/api';
import { formatTimecode, pluralize } from '../lib/format';

interface ScheduleEntry {
  date: string;
  platform: string;
  title: string;
  clip_id?: string;
  episode_id?: string;
  time?: string;
  kind?: 'clip' | 'longform';
}

const PLATFORM_COLOR: Record<string, string> = {
  youtube: '#ff3344',
  tiktok: '#69c9d0',
  instagram: '#e1306c',
  x: '#e8e8e8',
  linkedin: '#0a66c2',
  facebook: '#1877f2',
  threads: '#a0a0a0',
  pinterest: '#e60023',
  bluesky: '#1185fe',
  spotify: '#1db954',
};

export function Schedule(target: HTMLElement): void {
  const data = signal<UnknownRecord | null>(null);
  const err = signal<string | null>(null);

  (async () => {
    try {
      data.set(await api.schedule());
    } catch (e) {
      err.set((e as Error).message);
    }
  })();

  const page = h('div', { class: 'min-h-full' });

  effect(() => {
    const d = data();
    const e = err();
    if (e && !d) {
      page.replaceChildren(
        h(
          'div',
          {
            class:
              'max-w-[1280px] mx-auto px-10 py-16 text-body text-status-danger',
          },
          e
        )
      );
      return;
    }
    if (!d) {
      page.replaceChildren(
        h(
          'div',
          { class: 'max-w-[1280px] mx-auto px-10 py-10' },
          h('div', { class: 'panel h-96 animate-pulse-breath' })
        )
      );
      return;
    }
    page.replaceChildren(renderCalendar(d));
  });

  mount(target, page);
}

function renderCalendar(d: UnknownRecord): HTMLElement {
  const entries = (d.entries as ScheduleEntry[]) ?? [];
  const days = groupByDay(entries);
  const total = entries.length;

  return h(
    'div',
    { class: 'max-w-[1400px] mx-auto px-10 py-10' },
    h(
      'header',
      { class: 'flex items-baseline justify-between mb-10' },
      h(
        'div',
        null,
        h(
          'h1',
          { class: 'font-display text-display-xl text-ink-primary' },
          'Schedule'
        ),
        h(
          'p',
          { class: 'text-body text-ink-secondary mt-2' },
          total > 0
            ? `${pluralize(total, 'post')} queued across the next seven days.`
            : 'Nothing queued for the next seven days.'
        )
      )
    ),
    days.length === 0
      ? h(
          'div',
          { class: 'panel p-16 text-center' },
          h(
            'div',
            {
              class: 'font-display text-display-md text-ink-secondary mb-3',
            },
            'Quiet week.'
          ),
          h(
            'p',
            { class: 'text-body text-ink-tertiary max-w-md mx-auto' },
            'Once you approve clips they land on this calendar. The cascade scheduler spaces them per config/config.toml.'
          )
        )
      : h(
          'div',
          {
            class: 'grid gap-3',
            style: {
              gridTemplateColumns: `repeat(${days.length}, minmax(0, 1fr))`,
            },
          },
          ...days.map(renderDayColumn)
        )
  );
}

function groupByDay(
  entries: ScheduleEntry[]
): Array<{ date: string; entries: ScheduleEntry[] }> {
  const map = new Map<string, ScheduleEntry[]>();
  for (const e of entries) {
    const bucket = map.get(e.date) ?? [];
    bucket.push(e);
    map.set(e.date, bucket);
  }
  return [...map.entries()]
    .map(([date, entries]) => ({ date, entries }))
    .sort((a, b) => a.date.localeCompare(b.date));
}

function renderDayColumn(day: {
  date: string;
  entries: ScheduleEntry[];
}): HTMLElement {
  const d = new Date(day.date);
  const weekday = d.toLocaleDateString(undefined, { weekday: 'short' });
  const monthDay = d.toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
  });

  return h(
    'div',
    { class: 'panel p-4 flex flex-col gap-2 min-h-[200px]' },
    h(
      'div',
      { class: 'pb-2 mb-2 border-b border-border-subtle' },
      h(
        'div',
        {
          class:
            'text-heading-sm uppercase text-ink-tertiary font-mono tabular',
        },
        weekday
      ),
      h(
        'div',
        { class: 'text-heading-md text-ink-primary font-display' },
        monthDay
      )
    ),
    ...day.entries.map(renderEntry)
  );
}

function renderEntry(e: ScheduleEntry): HTMLElement {
  const color = PLATFORM_COLOR[e.platform.toLowerCase()] ?? '#7a7466';
  return h(
    'div',
    {
      class:
        'px-2.5 py-2 rounded bg-surface-2 border border-border-subtle flex flex-col gap-1',
    },
    h(
      'div',
      { class: 'flex items-center gap-2' },
      h('span', {
        class: 'w-1.5 h-1.5 rounded-full shrink-0',
        style: { background: color },
      }),
      h(
        'span',
        {
          class:
            'text-code-sm text-ink-tertiary font-mono tabular uppercase tracking-wide',
        },
        e.platform
      ),
      e.time
        ? h(
            'span',
            {
              class:
                'text-code-sm text-ink-tertiary font-mono tabular ml-auto',
            },
            e.time
          )
        : null
    ),
    h(
      'div',
      { class: 'text-body-sm text-ink-primary leading-snug line-clamp-2' },
      e.title || 'Untitled'
    )
  );

  void formatTimecode;
}
