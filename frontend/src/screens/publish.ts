/**
 * Publish — sign off on where each clip lands, see the weekly lane-up, kick
 * the uploads. Pulls the episode's clips + per-platform metadata and shows
 * per-platform readiness. Click "Publish" to POST approve-publish; cascade
 * handles the actual uploads behind the scenes.
 */

import { h, mount } from '../lib/dom';
import { signal, effect, type Signal } from '../lib/signals';
import { api, type UnknownRecord } from '../lib/api';
import { describeStatus, episodeTitle, formatDuration, pluralize } from '../lib/format';
import { StatusPill } from '../components/StatusPill';
import { Button } from '../components/Button';
import { Icon } from '../components/icons';
import { link, navigate } from '../lib/router';
import { showToast } from '../state/ui';

const PLATFORMS = [
  { key: 'youtube', label: 'YouTube', color: '#ff3344' },
  { key: 'tiktok', label: 'TikTok', color: '#69c9d0' },
  { key: 'instagram', label: 'Instagram', color: '#e1306c' },
  { key: 'x', label: 'X', color: '#e8e8e8' },
  { key: 'linkedin', label: 'LinkedIn', color: '#0a66c2' },
  { key: 'facebook', label: 'Facebook', color: '#1877f2' },
  { key: 'threads', label: 'Threads', color: '#a0a0a0' },
  { key: 'pinterest', label: 'Pinterest', color: '#e60023' },
  { key: 'bluesky', label: 'Bluesky', color: '#1185fe' },
];

export function Publish(target: HTMLElement, episodeId: string): void {
  const episode = signal<UnknownRecord | null>(null);
  const clips = signal<UnknownRecord[] | null>(null);
  const err = signal<string | null>(null);
  const publishing = signal<boolean>(false);

  async function load(): Promise<void> {
    try {
      const [ep, cs] = await Promise.all([
        api.getEpisode(episodeId),
        api.listClips(episodeId),
      ]);
      episode.set(ep);
      clips.set(cs);
      err.set(null);
    } catch (e) {
      err.set((e as Error).message);
    }
  }

  void load();

  const page = h('div', { class: 'min-h-full flex flex-col' });

  effect(() => {
    const ep = episode();
    const cs = clips();
    const e = err();

    if (e && !ep) {
      page.replaceChildren(
        h('div', { class: 'px-10 py-10 text-status-danger' }, e)
      );
      return;
    }
    if (!ep || !cs) {
      page.replaceChildren(
        h(
          'div',
          { class: 'px-10 py-10' },
          h('div', { class: 'panel h-96 animate-pulse-breath' })
        )
      );
      return;
    }

    const approved = cs.filter((c) => c.status === 'approved' || c.status === 'published');
    const pending = cs.filter((c) => c.status !== 'rejected' && c.status !== 'approved' && c.status !== 'published');
    const rejected = cs.filter((c) => c.status === 'rejected');

    page.replaceChildren(
      renderHeader(episodeId, ep),
      h(
        'div',
        {
          class:
            'max-w-[1200px] mx-auto w-full px-8 py-6 flex flex-col gap-6',
        },
        renderOverview(approved.length, pending.length, rejected.length, ep),
        renderPlatforms(cs),
        renderClipList(approved, rejected, pending)
      ),
      renderPublishBar(episodeId, ep, approved.length, publishing)
    );
  });

  mount(target, page);
}

function renderHeader(
  episodeId: string,
  ep: UnknownRecord
): HTMLElement {
  const status = describeStatus(ep.status as string);
  const title = episodeTitle(ep, episodeId);

  return h(
    'header',
    {
      class:
        'sticky top-0 z-10 bg-canvas border-b border-border-subtle px-8 py-4 flex items-center gap-5',
    },
    h(
      'a',
      {
        ...link(`/episodes/${episodeId}`),
        class:
          'w-8 h-8 flex items-center justify-center rounded-md text-ink-tertiary hover:text-ink-primary hover:bg-surface-2',
      },
      Icon.chevronLeft()
    ),
    h(
      'div',
      { class: 'flex-1 min-w-0' },
      h(
        'div',
        { class: 'flex items-center gap-3' },
        h(
          'span',
          { class: 'text-heading-sm uppercase text-ink-tertiary' },
          'Publish'
        ),
        StatusPill({ descriptor: status, size: 'sm' })
      ),
      h(
        'div',
        { class: 'text-body-lg text-ink-primary font-medium mt-1 truncate' },
        title
      )
    )
  );
}

function renderOverview(
  approved: number,
  pending: number,
  rejected: number,
  ep: UnknownRecord
): HTMLElement {
  const longformUrl = (ep.youtube_longform_url as string) ?? '';
  return h(
    'div',
    { class: 'panel p-6 flex items-center gap-10 flex-wrap' },
    statTile('Ready to post', String(approved), 'status-success'),
    statTile('Pending review', String(pending), 'ink-primary'),
    statTile('Rejected', String(rejected), 'ink-secondary'),
    statTile(
      'Longform',
      longformUrl ? 'YouTube uploaded' : 'Not uploaded',
      longformUrl ? 'status-success' : 'ink-secondary'
    ),
    statTile(
      'Duration',
      formatDuration(ep.duration_seconds as number),
      'ink-primary'
    )
  );
}

function statTile(label: string, value: string, tone: string): HTMLElement {
  return h(
    'div',
    null,
    h(
      'div',
      { class: 'text-heading-sm uppercase text-ink-tertiary mb-1' },
      label
    ),
    h(
      'div',
      {
        class: `text-display-md font-display text-${tone} font-mono tabular`,
      },
      value
    )
  );
}

function renderPlatforms(clips: UnknownRecord[]): HTMLElement {
  return h(
    'div',
    { class: 'panel p-5' },
    h(
      'div',
      { class: 'text-heading-sm uppercase text-ink-tertiary mb-4' },
      'Per-platform readiness'
    ),
    h(
      'div',
      {
        class:
          'grid grid-cols-3 gap-3',
      },
      ...PLATFORMS.map((p) => {
        const filled = clips.filter((c) => {
          const m = (c.metadata as Record<string, UnknownRecord> | undefined)?.[p.key];
          return (
            m &&
            Object.values(m).some((v) => typeof v === 'string' && v.length > 0)
          );
        }).length;
        const total = clips.length;
        const complete = filled === total && total > 0;
        return h(
          'div',
          {
            class:
              'flex items-center gap-3 px-3 py-2.5 rounded-md bg-surface-2 border border-border-subtle',
          },
          h('span', {
            class: 'w-2 h-2 rounded-full',
            style: { background: p.color },
          }),
          h(
            'span',
            { class: 'flex-1 text-body text-ink-primary font-medium' },
            p.label
          ),
          h(
            'span',
            {
              class: [
                'text-code font-mono tabular',
                complete ? 'text-status-success' : 'text-ink-tertiary',
              ].join(' '),
            },
            `${filled}/${total}`
          )
        );
      })
    )
  );
}

function renderClipList(
  approved: UnknownRecord[],
  rejected: UnknownRecord[],
  pending: UnknownRecord[]
): HTMLElement {
  return h(
    'div',
    { class: 'panel p-5 flex flex-col gap-3' },
    h(
      'div',
      { class: 'text-heading-sm uppercase text-ink-tertiary' },
      `Clips · ${pluralize(approved.length + pending.length + rejected.length, 'total')}`
    ),
    pending.length > 0
      ? h(
          'p',
          { class: 'text-body text-status-warning' },
          `${pluralize(pending.length, 'clip')} still pending review — head back to clip review before publishing.`
        )
      : null,
    h(
      'ul',
      { class: 'flex flex-col divide-y divide-border-subtle' },
      ...[...approved, ...pending, ...rejected].map((c) =>
        h(
          'li',
          {
            class: 'flex items-center gap-3 py-2.5',
          },
          h('span', {
            class: [
              'w-2 h-2 rounded-full',
              c.status === 'approved' || c.status === 'published'
                ? 'bg-status-success'
                : c.status === 'rejected'
                ? 'bg-status-danger'
                : 'bg-status-warning',
            ].join(' '),
          }),
          h(
            'span',
            { class: 'text-body text-ink-primary flex-1 truncate' },
            (c.title as string) || 'Untitled clip'
          ),
          h(
            'span',
            {
              class: 'text-code-sm text-ink-tertiary font-mono tabular',
            },
            formatDuration(c.duration as number)
          )
        )
      )
    )
  );
}

function renderPublishBar(
  episodeId: string,
  ep: UnknownRecord,
  approvedCount: number,
  publishing: Signal<boolean>
): HTMLElement {
  const status = describeStatus(ep.status as string);
  const canPublish =
    status.key === 'awaiting_publish' ||
    status.key === 'awaiting_clip_review';

  const host = h('footer', {
    class:
      'sticky bottom-0 z-20 border-t border-border-subtle bg-canvas/95 backdrop-blur-md px-8 py-4',
  });

  effect(() => {
    const p = publishing();
    host.replaceChildren(
      h(
        'div',
        {
          class: 'max-w-[1200px] mx-auto flex items-center gap-4',
        },
        h(
          'div',
          { class: 'flex-1' },
          h(
            'div',
            { class: 'text-body text-ink-primary font-medium' },
            canPublish
              ? `${approvedCount} approved clip${approvedCount === 1 ? '' : 's'} ready`
              : status.label
          ),
          h(
            'div',
            { class: 'text-body-sm text-ink-secondary' },
            canPublish
              ? 'Publishing schedules the uploads across all platforms.'
              : status.hint
          )
        ),
        canPublish && approvedCount > 0
          ? Button({
              variant: 'primary',
              size: 'lg',
              label: p ? 'Kicking publish…' : 'Publish everywhere',
              loading: p,
              onClick: async () => {
                publishing.set(true);
                try {
                  await api.approvePublish(episodeId);
                  showToast('Publish kicked off.', 'success');
                  navigate(`/episodes/${episodeId}`);
                } catch (e) {
                  showToast((e as Error).message, 'error');
                } finally {
                  publishing.set(false);
                }
              },
            })
          : null
      )
    );
  });

  return host;
}
