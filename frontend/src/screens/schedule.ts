/**
 * Schedule — current approved release proposals and recorded publication evidence.
 */

import { h, mount } from '../lib/dom';
import { signal, effect } from '../lib/signals';
import { api, type UnknownRecord } from '../lib/api';
import { pluralize } from '../lib/format';
import { link } from '../lib/router';

interface ScheduleItem {
  type: 'longform' | 'short' | string;
  episode_id: string;
  name?: string;
  title?: string;
  scheduled_date: string;
  destination?: string;
  destinations?: string[];
  clip_id?: string;
  scheduled_time?: string;
}

interface ScheduleDay {
  date: string;
  day_name: string;
  items: ScheduleItem[];
}

interface PublicationEvidence {
  episode_id: string;
  name?: string;
  content_type: 'podcast_audio' | 'longform' | 'short' | string;
  destination?: string;
  destinations?: string[];
  status: string;
  clip_id?: string;
  url?: string;
}

const TYPE_COLOR: Record<string, string> = {
  longform: '#6fcf8e',
  short: '#f5a524',
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
  const publicationEvidence =
    (d.publication_evidence as PublicationEvidence[]) ?? [];

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
            ? `Suggested slots for ${pluralize(total, 'post')}. Confirm release dates in your publishing service.`
            : 'No release suggestions for the next seven days.'
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
            'Current, approved episodes and clips appear here as a draft release plan.'
          )
        )
      : h(
          'div',
          {
            class: 'grid gap-3',
            style: {
              gridTemplateColumns: 'repeat(auto-fit, minmax(170px, 1fr))',
            },
          },
          ...days.map(renderDayColumn)
        ),
    publicationEvidence.length > 0
      ? renderPublicationEvidence(publicationEvidence)
      : null
  );
}

function renderDayColumn(day: ScheduleDay): HTMLElement {
  const dateObj = new Date(day.date + 'T00:00:00');
  const dayOfMonth = dateObj.toLocaleDateString(undefined, {
    day: 'numeric',
  });
  const weekday = day.day_name?.slice(0, 3) || '';
  const isToday = day.date === new Date().toLocaleDateString('en-CA');

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
        { class: 'flex items-baseline justify-between' },
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
          { class: 'text-display-md font-display text-ink-primary' },
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
          isToday ? 'Open day' : 'No suggested posts'
        )
      : h('div', { class: 'flex flex-col gap-2' }, ...day.items.map(renderItem))
  );
}

function renderItem(item: ScheduleItem): HTMLElement {
  const color = TYPE_COLOR[item.type] ?? '#7a7466';
  const typeLabel =
    item.type === 'longform'
      ? 'Longform'
      : item.type === 'short'
      ? 'Short'
      : item.type;
  const destinations =
    item.destinations ?? (item.destination ? [item.destination] : []);
  return h(
    'a',
    {
      ...link(`/episodes/${item.episode_id}`),
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
        : destinations.length > 0
        ? h(
            'span',
            {
              class:
                'text-code-sm text-ink-tertiary font-mono tabular ml-auto uppercase',
            },
            destinations.join(', ')
          )
        : null
    ),
    h(
      'div',
      { class: 'text-body-sm text-ink-primary leading-snug line-clamp-3' },
      item.title || item.name || 'Untitled'
    )
  );
}

function renderPublicationEvidence(records: PublicationEvidence[]): HTMLElement {
  const byEpisode = new Map<string, PublicationEvidence[]>();
  for (const record of records) {
    const existing = byEpisode.get(record.episode_id) ?? [];
    existing.push(record);
    byEpisode.set(record.episode_id, existing);
  }
  return h(
    'section',
    { class: 'mt-10' },
    h(
      'div',
      { class: 'mb-4' },
      h(
        'h2',
        { class: 'font-display text-display-md text-ink-primary' },
        'Recorded publication activity'
      ),
      h(
        'p',
        { class: 'text-body-sm text-ink-tertiary mt-1' },
        'These records are excluded from generic suggestions. A submission record does not confirm a live post.'
      )
    ),
    h(
      'div',
      { class: 'grid gap-3 md:grid-cols-2' },
      ...Array.from(byEpisode.entries()).map(([episodeId, episodeRecords]) =>
        h(
          'div',
          { class: 'panel p-4' },
          h(
            'a',
            {
              ...link(`/episodes/${episodeId}`),
              class: 'text-heading-sm text-ink-primary hover:text-accent',
            },
            episodeRecords[0]?.name || episodeId
          ),
          h(
            'div',
            { class: 'mt-3 flex flex-col gap-2' },
            ...episodeRecords.map(renderPublicationRecord)
          )
        )
      )
    )
  );
}

function renderPublicationRecord(record: PublicationEvidence): HTMLElement {
  const content =
    record.content_type === 'podcast_audio'
      ? 'Podcast RSS audio'
      : record.content_type === 'longform'
      ? 'Longform'
      : record.clip_id
      ? `Short ${record.clip_id}`
      : 'Short';
  const destinations =
    record.destinations ??
    (record.destination ? [record.destination] : ['unknown destination']);
  const destinationLabel = destinations
    .map((value) => value.replaceAll('_', ' '))
    .join(', ');
  const status =
    record.status === 'published'
      ? 'Published URL recorded'
      : record.status === 'submitted'
      ? 'Submission recorded'
      : record.status === 'already_submitted'
      ? 'Prior submission recorded'
      : 'Publication record';
  const safeUrl =
    record.url?.startsWith('https://') || record.url?.startsWith('http://')
      ? record.url
      : null;
  const label = h(
    'span',
    { class: 'text-body-sm text-ink-secondary' },
    `${content} · ${destinationLabel} · ${status}`
  );
  return safeUrl
    ? h(
        'a',
        {
          href: safeUrl,
          target: '_blank',
          rel: 'noreferrer',
          class: 'hover:text-accent',
        },
        label
      )
    : label;
}
