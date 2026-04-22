/**
 * Schedule — seven-day publish calendar. Pulls /api/schedule and lays
 * clips into per-day columns with a platform-colored pill per entry.
 */

import { h, mount } from '../lib/dom';
import { signal, effect } from '../lib/signals';
import { api, type UnknownRecord } from '../lib/api';
import { formatTimecode, pluralize } from '../lib/format';

interface ScheduleItem {
  type: 'longform' | 'clip' | string;
  episode_id: string;
  name?: string;
  title?: string;
  scheduled_date: string;
  platform?: string;
  clip_id?: string;
  virality_score?: number;
  scheduled_time?: string;
}

interface ScheduleDay {
  date: string;
  day_name: string;
  items: ScheduleItem[];
}

const TYPE_COLOR: Record<string, string> = {
  longform: '#6fcf8e',
  clip: '#f5a524',
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
  const days = (d.schedule as ScheduleDay[]) ?? [];
  const total = (d.total_items as number) ?? 0;
  const unscheduledShorts = (d.unscheduled_shorts as number) ?? 0;
  const unscheduledLongforms = (d.unscheduled_longforms as number) ?? 0;
  const unscheduled = unscheduledShorts + unscheduledLongforms;

  return h(
    'div',
    { class: 'max-w-[1400px] mx-auto px-10 py-10' },
    h(
      'header',
      { class: 'flex items-baseline justify-between mb-8' },
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
      ),
      unscheduled > 0
        ? h(
            'div',
            {
              class:
                'panel px-5 py-3 border-status-warning/30 text-body-sm text-status-warning',
            },
            `${pluralize(unscheduled, 'post')} waiting for a slot.`
          )
        : null
    ),
    total === 0
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

  void formatTimecode;
}

function renderDayColumn(day: ScheduleDay): HTMLElement {
  const dateObj = new Date(day.date + 'T00:00:00');
  const dayOfMonth = dateObj.toLocaleDateString(undefined, {
    day: 'numeric',
  });
  const weekday = day.day_name?.slice(0, 3) || '';
  const isToday = day.date === new Date().toISOString().slice(0, 10);

  return h(
    'div',
    {
      class: [
        'panel p-4 flex flex-col gap-2 min-h-[220px]',
        isToday ? 'border-accent/40' : '',
      ].join(' '),
    },
    h(
      'div',
      { class: 'pb-2 mb-2 border-b border-border-subtle' },
      h(
        'div',
        {
          class: 'flex items-baseline justify-between',
        },
        h(
          'span',
          {
            class: [
              'text-heading-sm uppercase font-mono tabular',
              isToday ? 'text-accent' : 'text-ink-tertiary',
            ].join(' '),
          },
          weekday
        ),
        h(
          'span',
          {
            class: 'text-display-md font-display text-ink-primary',
          },
          dayOfMonth
        )
      )
    ),
    day.items.length === 0
      ? h(
          'div',
          {
            class:
              'flex-1 flex items-center justify-center text-body-sm text-ink-tertiary/70 italic',
          },
          isToday ? 'Open day — nothing queued.' : 'No posts'
        )
      : h(
          'div',
          { class: 'flex flex-col gap-2' },
          ...day.items.map(renderItem)
        )
  );
}

function renderItem(item: ScheduleItem): HTMLElement {
  const color = TYPE_COLOR[item.type] ?? '#7a7466';
  const typeLabel =
    item.type === 'longform'
      ? 'Longform'
      : item.type === 'clip'
      ? 'Short'
      : item.type;
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
        typeLabel
      ),
      item.scheduled_time
        ? h(
            'span',
            {
              class:
                'text-code-sm text-ink-tertiary font-mono tabular ml-auto',
            },
            item.scheduled_time
          )
        : null
    ),
    h(
      'div',
      {
        class: 'text-body-sm text-ink-primary leading-snug line-clamp-3',
      },
      item.title || item.name || 'Untitled'
    )
  );
}
