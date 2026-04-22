/**
 * Longform Review — watch the rendered episode, request edits in natural
 * language, approve when it's right.
 *
 * The video is served from /media/episodes/:id/longform.mp4. Edits live in
 * episode.longform_edits[] (cut / trim_start / trim_end). Each edit is
 * rendered as a colored lane on the duration timeline below the player.
 * The freeform input POSTs to /api/episodes/:id/chat so the agent can
 * parse "trim the first 2 minutes and cut the strip-club story around 42"
 * into structured edits.
 */

import { h, mount } from '../lib/dom';
import { signal, effect, type Signal } from '../lib/signals';
import { api, type UnknownRecord } from '../lib/api';
import { describeStatus, episodeTitle, formatDuration, formatTimecode } from '../lib/format';
import { StatusPill } from '../components/StatusPill';
import { Button } from '../components/Button';
import { Icon } from '../components/icons';
import { link, navigate } from '../lib/router';
import { showToast } from '../state/ui';

interface Edit {
  type: 'cut' | 'trim_start' | 'trim_end';
  start_seconds?: number;
  end_seconds?: number;
  seconds?: number;
  reason?: string;
}

export function LongformReview(target: HTMLElement, episodeId: string): void {
  const episode = signal<UnknownRecord | null>(null);
  const edits = signal<Edit[]>([]);
  const chatSending = signal<boolean>(false);
  const error = signal<string | null>(null);
  // Held ref so the cut timeline can seek the longform player on click.
  const videoRef: { el: HTMLVideoElement | null } = { el: null };

  async function load(): Promise<void> {
    try {
      const [ep, es] = await Promise.all([
        api.getEpisode(episodeId),
        api.listEdits(episodeId),
      ]);
      episode.set(ep);
      edits.set((es.edits as unknown) as Edit[]);
      error.set(null);
    } catch (e) {
      error.set((e as Error).message);
    }
  }

  void load();

  const page = h('div', { class: 'min-h-full flex flex-col' });

  effect(() => {
    const ep = episode();
    const err = error();
    if (err && !ep) {
      page.replaceChildren(
        h('div', { class: 'px-10 py-10 text-status-danger' }, err)
      );
      return;
    }
    if (!ep) {
      page.replaceChildren(loadingState());
      return;
    }
    page.replaceChildren(
      renderHeader(episodeId, ep),
      h(
        'div',
        { class: 'max-w-[1200px] mx-auto px-8 py-6 flex flex-col gap-6 w-full' },
        renderPlayer(episodeId, ep, videoRef),
        renderTimeline(ep, edits(), videoRef),
        renderEditsList(episodeId, edits, load),
        renderEditInput(episodeId, chatSending, load)
      ),
      renderApprovalBar(episodeId, ep, edits())
    );
  });

  mount(target, page);
}

function loadingState(): HTMLElement {
  return h(
    'div',
    { class: 'px-10 py-10' },
    h('div', { class: 'panel h-96 animate-pulse-breath' })
  );
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
          'Longform review'
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

function renderPlayer(
  episodeId: string,
  ep: UnknownRecord,
  videoRef: { el: HTMLVideoElement | null }
): HTMLElement {
  const status = describeStatus(ep.status as string);
  const hasLongform =
    status.key === 'awaiting_longform_review' ||
    status.key === 'awaiting_clip_review' ||
    status.key === 'awaiting_publish' ||
    status.key === 'awaiting_backup' ||
    status.key === 'live';

  if (!hasLongform) {
    return h(
      'div',
      { class: 'panel p-16 text-center' },
      h(
        'div',
        { class: 'font-display text-display-md text-ink-secondary mb-3' },
        'Longform render isn’t ready.'
      ),
      h(
        'p',
        { class: 'text-body text-ink-tertiary max-w-md mx-auto' },
        'Cascade renders the longform after crop setup. You’ll see the player here when it’s done.'
      )
    );
  }

  const video = h('video', {
    src: `/media/episodes/${episodeId}/longform.mp4`,
    poster: `/api/episodes/${episodeId}/crop-frame`,
    controls: true,
    preload: 'metadata',
    class: 'w-full bg-black',
    style: { maxHeight: '64vh' },
  }) as HTMLVideoElement;
  videoRef.el = video;

  return h(
    'div',
    { class: 'panel overflow-hidden' },
    video
  );
}

function renderTimeline(
  ep: UnknownRecord,
  edits: Edit[],
  videoRef: { el: HTMLVideoElement | null }
): HTMLElement {
  const duration = (ep.duration_seconds as number) ?? 0;
  if (duration <= 0) return h('div');

  const seekTo = (seconds: number): void => {
    const v = videoRef.el;
    if (!v) return;
    v.currentTime = Math.max(0, Math.min(seconds, duration));
    v.play().catch(() => {});
  };

  const lanes = edits.map((e, i) => {
    const start =
      e.type === 'trim_start'
        ? 0
        : e.type === 'trim_end'
        ? Math.max(0, duration - (e.seconds ?? 0))
        : e.start_seconds ?? 0;
    const end =
      e.type === 'trim_start'
        ? e.seconds ?? 0
        : e.type === 'trim_end'
        ? duration
        : e.end_seconds ?? 0;
    const leftPct = (start / duration) * 100;
    const widthPct = Math.max(0.6, ((end - start) / duration) * 100);
    const tone =
      e.type === 'cut'
        ? 'bg-status-danger/70'
        : e.type === 'trim_start'
        ? 'bg-accent/60'
        : 'bg-accent/60';
    return h('button', {
      class: `absolute top-0 bottom-0 rounded ${tone} hover:brightness-125 transition-[filter] duration-[120ms]`,
      style: {
        left: `${leftPct}%`,
        width: `${widthPct}%`,
      },
      title: `${e.type} · ${formatTimecode(start)}–${formatTimecode(end)}${
        e.reason ? ` · ${e.reason}` : ''
      }\nClick to seek there.`,
      dataset: { idx: String(i) },
      onclick: (ev: MouseEvent) => {
        ev.stopPropagation();
        seekTo(start);
      },
    });
  });

  return h(
    'div',
    { class: 'panel p-5' },
    h(
      'div',
      { class: 'flex items-baseline justify-between mb-3' },
      h(
        'span',
        { class: 'text-heading-sm uppercase text-ink-tertiary' },
        'Cut timeline'
      ),
      h(
        'span',
        { class: 'text-body-sm text-ink-tertiary font-mono tabular' },
        `${formatDuration(duration)} · ${edits.length} edit${edits.length === 1 ? '' : 's'}`
      )
    ),
    h(
      'div',
      {
        class:
          'relative h-9 rounded bg-surface-inset border border-border-subtle cursor-pointer',
        onclick: (ev: MouseEvent) => {
          const rect = (ev.currentTarget as HTMLElement).getBoundingClientRect();
          const frac = (ev.clientX - rect.left) / rect.width;
          seekTo(frac * duration);
        },
        title: 'Click to seek',
      },
      ...lanes
    ),
    h(
      'div',
      {
        class:
          'flex justify-between text-code-sm text-ink-tertiary font-mono tabular mt-1.5',
      },
      h('span', null, '0:00'),
      h('span', null, formatTimecode(duration / 2)),
      h('span', null, formatTimecode(duration))
    )
  );
}

function renderEditsList(
  episodeId: string,
  edits: Signal<Edit[]>,
  reload: () => Promise<void>
): HTMLElement {
  const host = h('div');
  effect(() => {
    const list = edits();
    if (list.length === 0) {
      host.replaceChildren(
        h(
          'div',
          {
            class:
              'panel p-5 text-body text-ink-tertiary italic',
          },
          'No edits yet — ask below if you want a trim or a cut.'
        )
      );
      return;
    }
    host.replaceChildren(
      h(
        'div',
        { class: 'panel divide-y divide-border-subtle' },
        ...list.map((e, i) => renderEditRow(episodeId, e, i, reload))
      )
    );
  });
  return host;
}

function renderEditRow(
  episodeId: string,
  e: Edit,
  i: number,
  reload: () => Promise<void>
): HTMLElement {
  const label =
    e.type === 'cut'
      ? `Cut ${formatTimecode(e.start_seconds ?? 0)}–${formatTimecode(e.end_seconds ?? 0)}`
      : e.type === 'trim_start'
      ? `Trim first ${formatDuration(e.seconds ?? 0)}`
      : `Trim last ${formatDuration(e.seconds ?? 0)}`;

  return h(
    'div',
    {
      class: 'flex items-center gap-4 px-5 py-3',
    },
    h(
      'span',
      { class: 'text-code-sm text-ink-tertiary font-mono tabular w-6' },
      `${i + 1}`
    ),
    h(
      'div',
      { class: 'flex-1' },
      h('div', { class: 'text-body text-ink-primary font-medium' }, label),
      e.reason
        ? h(
            'p',
            { class: 'text-body-sm text-ink-secondary mt-0.5' },
            e.reason
          )
        : null
    ),
    h(
      'button',
      {
        class:
          'text-ink-tertiary hover:text-status-danger text-code-sm hover:underline',
        onclick: async () => {
          try {
            await api.removeEdit(episodeId, i);
            showToast('Edit removed.');
            await reload();
          } catch (err) {
            showToast((err as Error).message, 'error');
          }
        },
      },
      'remove'
    )
  );
}

function renderEditInput(
  episodeId: string,
  sending: Signal<boolean>,
  reload: () => Promise<void>
): HTMLElement {
  const input = h('textarea', {
    class: [
      'w-full bg-surface-2 border border-border rounded-lg px-4 py-3',
      'text-body text-ink-primary placeholder:text-ink-disabled',
      'resize-none focus:border-accent focus:outline-none leading-relaxed',
    ].join(' '),
    rows: '3',
    placeholder:
      'Trim the first 2 minutes. Cut the strip-club story around 42:00. Remove the coughing fit around 1:15:30.',
  }) as HTMLTextAreaElement;

  const submitBtn = h('div');
  effect(() => {
    submitBtn.replaceChildren(
      Button({
        variant: 'primary',
        size: 'md',
        label: sending() ? 'Working…' : 'Propose edits',
        loading: sending(),
        onClick: async () => {
          const msg = input.value.trim();
          if (!msg) return;
          sending.set(true);
          try {
            const res = await api.chat(
              episodeId,
              `Please propose longform edits based on this request: ${msg}`
            );
            input.value = '';
            if (res.actions_taken && res.actions_taken.length > 0) {
              showToast(
                `${res.actions_taken.length} edit(s) added.`,
                'success'
              );
            } else {
              showToast(res.response.slice(0, 200));
            }
            await reload();
          } catch (e) {
            showToast((e as Error).message, 'error');
          } finally {
            sending.set(false);
          }
        },
      })
    );
  });

  return h(
    'div',
    { class: 'panel p-5 flex flex-col gap-3' },
    h(
      'div',
      { class: 'flex items-baseline justify-between' },
      h(
        'span',
        { class: 'text-heading-sm uppercase text-ink-tertiary' },
        'Request an edit'
      ),
      h(
        'span',
        { class: 'text-body-sm text-ink-tertiary' },
        'Use plain language — cascade parses it into cuts.'
      )
    ),
    input,
    h('div', { class: 'flex items-center justify-end gap-2' }, submitBtn)
  );
}

function renderApprovalBar(
  episodeId: string,
  ep: UnknownRecord,
  edits: Edit[]
): HTMLElement {
  const status = describeStatus(ep.status as string);
  const pending = edits.length > 0;
  const canApprove = status.key === 'awaiting_longform_review';
  const alreadyPast =
    status.key === 'awaiting_clip_review' ||
    status.key === 'awaiting_publish' ||
    status.key === 'awaiting_backup' ||
    status.key === 'live';

  const headline = pending
    ? `${edits.length} edit${edits.length === 1 ? '' : 's'} queued`
    : canApprove
    ? 'Happy with this cut?'
    : alreadyPast
    ? 'Longform is already approved'
    : status.label;
  const sub = pending
    ? 'Apply them to re-render before approving.'
    : canApprove
    ? 'Approving uploads to YouTube, updates the RSS feed, and fires clip mining.'
    : alreadyPast
    ? 'Downstream work has started. Request edits here to re-open.'
    : status.hint;

  return h(
    'footer',
    {
      class:
        'sticky bottom-0 z-20 border-t border-border-subtle bg-canvas/95 backdrop-blur-md px-8 py-4',
    },
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
          headline
        ),
        h(
          'div',
          { class: 'text-body-sm text-ink-secondary' },
          sub
        )
      ),
      pending
        ? Button({
            variant: 'secondary',
            size: 'lg',
            label: 'Apply edits & re-render',
            onClick: async () => {
              try {
                await api.applyEdits(episodeId);
                showToast('Re-render queued.', 'success');
                navigate(`/episodes/${episodeId}`);
              } catch (e) {
                showToast((e as Error).message, 'error');
              }
            },
          })
        : null,
      !pending && canApprove
        ? Button({
            variant: 'primary',
            size: 'lg',
            label: 'Approve longform',
            onClick: async () => {
              try {
                await api.approveLongform(episodeId);
                showToast('Longform approved — clip mining begins.', 'success');
                navigate(`/episodes/${episodeId}`);
              } catch (e) {
                showToast((e as Error).message, 'error');
              }
            },
          })
        : null
    )
  );
}
