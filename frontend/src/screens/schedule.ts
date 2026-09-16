/**
 * Schedule — current approved release proposals and recorded publication evidence.
 */

import { h, mount } from '../lib/dom';
import { signal, effect } from '../lib/signals';
import { api, type UnknownRecord } from '../lib/api';
import { pluralize } from '../lib/format';
import { link } from '../lib/router';
import {
  distributionVersionLabel,
  publicationEvidenceStatusLabel,
} from '../lib/clip-review-surface';

interface ScheduleItem {
  type: 'longform' | 'short' | string;
  episode_id: string;
  name?: string;
  title?: string;
  scheduled_date: string;
  destination?: string;
  destinations?: string[];
  clip_id?: string;
  planned_date?: string;
  state?:
    | 'planned'
    | 'scheduled'
    | 'cancellation_pending'
    | 'failed'
    | 'unknown'
    | 'suggested';
  job_id?: string;
  request_id?: string;
  error?: string;
  artifact_current?: boolean | null;
  version?: 'base' | 'background_motion_v1' | 'gameplay_surround_v1';
  variant_id?: null | 'background_motion_v1' | 'gameplay_surround_v1';
}

interface ScheduleDay {
  date: string;
  day_name: string;
  items: ScheduleItem[];
}

interface HeldEpisode {
  episode_id: string;
  name?: string;
  blockers?: string[];
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
  scheduled?: boolean;
  job_id?: string;
  request_id?: string;
  error?: string;
  version?: 'base' | 'background_motion_v1' | 'gameplay_surround_v1';
  variant_id?: null | 'background_motion_v1' | 'gameplay_surround_v1';
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
  const heldItems = (d.held_items as HeldEpisode[]) ?? [];
  const timezone = (d.timezone as string) || undefined;
  const summary =
    total > 0
      ? `${pluralize(total, 'post')} with current release dates and states.`
      : 'No releasable posts are on the calendar.';

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
          `${summary}${timezone ? ` Timezone: ${timezone}.` : ''}`
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
            heldItems.length
              ? 'Release checks are holding the work listed below.'
              : 'Current, approved episodes and clips appear here as a release plan.'
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
          ...days.map((day) => renderDayColumn(day, timezone))
        ),
    heldItems.length ? renderHeldItems(heldItems) : null,
    publicationEvidence.length > 0
      ? renderPublicationEvidence(publicationEvidence)
      : null
  );
}

function renderDayColumn(day: ScheduleDay, timezone?: string): HTMLElement {
  const dateObj = new Date(day.date + 'T00:00:00');
  const dayOfMonth = dateObj.toLocaleDateString(undefined, {
    day: 'numeric',
  });
  const month = dateObj
    .toLocaleDateString(undefined, { month: 'short' })
    .toUpperCase();
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
          `${month} ${dayOfMonth}`
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
      : h(
          'div',
          { class: 'flex flex-col gap-2' },
          ...day.items.map((item) => renderItem(item, timezone))
        )
  );
}

function renderItem(item: ScheduleItem, timezone?: string): HTMLElement {
  const color = TYPE_COLOR[item.type] ?? '#7a7466';
  const typeLabel =
    item.type === 'longform'
      ? 'Longform'
      : item.type === 'short'
      ? 'Short'
      : item.type;
  const destinations =
    item.destinations ?? (item.destination ? [item.destination] : []);
  const state = item.state ?? 'suggested';
  const stateLabel =
    state === 'cancellation_pending'
      ? 'Cancellation pending verification'
      : state;
  const stateClass =
    state === 'scheduled'
      ? 'text-status-success'
      : state === 'failed'
        ? 'text-status-danger'
        : state === 'unknown' || state === 'cancellation_pending'
          ? 'text-status-warning'
          : state === 'planned'
            ? 'text-accent'
            : 'text-ink-tertiary';
  return h(
    'a',
    {
      ...link(
        item.type === 'short' && item.clip_id
          ? `/episodes/${encodeURIComponent(item.episode_id)}/clips/review/${encodeURIComponent(item.clip_id)}`
          : `/episodes/${encodeURIComponent(item.episode_id)}`
      ),
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
      item.scheduled_date.includes('T')
        ? h(
            'span',
            {
              class:
                'text-code-sm text-ink-tertiary font-mono tabular ml-auto',
            },
            new Date(item.scheduled_date).toLocaleTimeString([], {
              hour: 'numeric',
              minute: '2-digit',
              timeZone: timezone,
            })
          )
        : null
    ),
    h(
      'div',
      { class: 'text-body-sm text-ink-primary leading-snug line-clamp-3' },
      item.title || item.name || 'Untitled'
    ),
    item.type === 'short'
      ? h(
          'div',
          { class: 'text-code-sm text-ink-secondary font-mono' },
          `Version · ${distributionVersionLabel(item.version, item.variant_id)}`
        )
      : null,
    destinations.length > 0
      ? h(
          'div',
          { class: 'text-code-sm text-ink-secondary font-mono uppercase' },
          `Destinations · ${destinations
            .map((value) => value.replaceAll('_', ' '))
            .join(', ')}`
        )
      : null,
    h(
      'div',
      { class: `text-code-sm font-mono uppercase ${stateClass}` },
      stateLabel
    ),
    item.job_id || item.request_id
      ? h(
          'div',
          { class: 'text-code-sm text-ink-tertiary font-mono break-all' },
          `${item.job_id ? 'Job' : 'Request'} ${item.job_id ?? item.request_id}`
        )
      : null,
    item.planned_date && item.planned_date !== item.scheduled_date
      ? h(
          'div',
          { class: 'text-code-sm text-status-warning font-mono' },
          `Planned ${new Date(item.planned_date).toLocaleString([], {
            timeZone: timezone,
          })}`
        )
      : null,
    item.artifact_current === false
      ? h(
          'div',
          { class: 'text-code-sm text-status-warning font-mono' },
          'Scheduled media differs from the current selected version'
        )
      : null,
    item.error
      ? h('div', { class: 'text-code-sm text-status-danger' }, item.error)
      : null
  );
}

function renderHeldItems(items: HeldEpisode[]): HTMLElement {
  return h(
    'section',
    { class: 'mt-10' },
    h(
      'h2',
      { class: 'font-display text-display-md text-ink-primary mb-4' },
      'Held by release checks'
    ),
    h(
      'div',
      { class: 'grid gap-3 md:grid-cols-2' },
      ...items.map((item) =>
        h(
          'div',
          { class: 'panel p-4 border-status-warning/30' },
          h(
            'a',
            {
              ...link(`/episodes/${item.episode_id}`),
              class: 'text-heading-sm text-ink-primary hover:text-accent',
            },
            item.name || item.episode_id
          ),
          h(
            'p',
            { class: 'text-body-sm text-status-warning mt-2' },
            'Release checks must pass before scheduling.'
          ),
          ...(item.blockers ?? [])
            .slice(0, 2)
            .map((message) =>
              h('p', { class: 'text-body-sm text-ink-secondary mt-1' }, message)
            )
        )
      )
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
      ? `Short ${record.clip_id} · ${distributionVersionLabel(
          record.version,
          record.variant_id
        )}`
      : 'Short';
  const destinations =
    record.destinations ??
    (record.destination ? [record.destination] : ['unknown destination']);
  const destinationLabel = destinations
    .map((value) => value.replaceAll('_', ' '))
    .join(', ');
  const status = publicationEvidenceStatusLabel(
    record.status,
    record.scheduled
  );
  const safeUrl =
    record.url?.startsWith('https://') || record.url?.startsWith('http://')
      ? record.url
      : null;
  const label = h(
    'span',
    { class: 'text-body-sm text-ink-secondary' },
    `${content} · ${destinationLabel} · ${status}${
      record.job_id
        ? ` · Job ${record.job_id}`
        : record.request_id
          ? ` · Request ${record.request_id}`
          : ''
    }${record.error ? ` · ${record.error}` : ''}`
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
