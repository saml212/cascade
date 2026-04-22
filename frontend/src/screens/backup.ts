/**
 * Backup — copy episode artifacts to the Seagate drive, then optionally
 * clear the SD card. Dangerous-action UI: approve-backup runs the backup
 * agent; SD clear is a separate step behind an explicit "I understand"
 * confirmation.
 */

import { h, mount } from '../lib/dom';
import { signal, effect } from '../lib/signals';
import { api, type UnknownRecord } from '../lib/api';
import { describeStatus, formatDuration } from '../lib/format';
import { StatusPill } from '../components/StatusPill';
import { Button } from '../components/Button';
import { Icon } from '../components/icons';
import { link, navigate } from '../lib/router';
import { showToast } from '../state/ui';

export function Backup(target: HTMLElement, episodeId: string): void {
  const episode = signal<UnknownRecord | null>(null);
  const approving = signal<boolean>(false);
  const confirmPhrase = signal<string>('');

  (async () => {
    try {
      episode.set(await api.getEpisode(episodeId));
    } catch (e) {
      showToast((e as Error).message, 'error');
    }
  })();

  const page = h('div', { class: 'min-h-full' });

  effect(() => {
    const ep = episode();
    if (!ep) {
      page.replaceChildren(
        h(
          'div',
          { class: 'px-10 py-10' },
          h('div', { class: 'panel h-64 animate-pulse-breath' })
        )
      );
      return;
    }
    page.replaceChildren(renderPage(episodeId, ep, approving, confirmPhrase));
  });

  mount(target, page);
}

function renderPage(
  episodeId: string,
  ep: UnknownRecord,
  approving: ReturnType<typeof signal<boolean>>,
  confirmPhrase: ReturnType<typeof signal<string>>
): HTMLElement {
  const status = describeStatus(ep.status as string);
  const title =
    (ep.guest_name as string)?.trim() ||
    (ep.episode_name as string)?.trim() ||
    episodeId;
  const canBackup = status.key === 'awaiting_backup';
  const expectedPhrase = 'back it up';

  const approveBtn = h('div');
  effect(() => {
    const busy = approving();
    const match = confirmPhrase().trim().toLowerCase() === expectedPhrase;
    approveBtn.replaceChildren(
      Button({
        variant: 'primary',
        size: 'lg',
        label: busy ? 'Backing up…' : 'Back it up',
        loading: busy,
        disabled: !match || busy,
        onClick: async () => {
          approving.set(true);
          try {
            await api.approveBackup(episodeId);
            showToast(
              'Backup started — pipeline is copying to Seagate.',
              'success'
            );
            navigate(`/episodes/${episodeId}`);
          } catch (e) {
            showToast((e as Error).message, 'error');
          } finally {
            approving.set(false);
          }
        },
      })
    );
  });

  const confirmInput = h('input', {
    type: 'text',
    placeholder: `Type "${expectedPhrase}" to enable the button`,
    class:
      'w-full h-11 bg-surface-2 border border-border rounded-md px-4 text-body text-ink-primary placeholder:text-ink-disabled focus:border-accent focus:outline-none',
    oninput: (e: Event) =>
      confirmPhrase.set((e.target as HTMLInputElement).value),
  });

  return h(
    'div',
    { class: 'max-w-[720px] mx-auto px-10 py-12 flex flex-col gap-6' },
    h(
      'a',
      {
        ...link(`/episodes/${episodeId}`),
        class:
          'inline-flex items-center gap-1 text-body-sm text-ink-tertiary hover:text-ink-primary mb-2',
      },
      Icon.chevronLeft({ size: 14 }),
      'Back to episode'
    ),
    h(
      'div',
      null,
      h(
        'div',
        { class: 'flex items-center gap-3 mb-2' },
        h(
          'span',
          { class: 'text-heading-sm uppercase text-ink-tertiary' },
          'Backup'
        ),
        StatusPill({ descriptor: status, size: 'sm' })
      ),
      h(
        'h1',
        { class: 'font-display text-display-xl text-ink-primary' },
        title
      )
    ),
    h(
      'div',
      { class: 'panel p-6 flex flex-col gap-4' },
      h(
        'p',
        { class: 'text-body-lg text-ink-secondary leading-relaxed' },
        'Cascade copies this episode — source footage, audio tracks, stitched video, longform render, all shorts, thumbnails, metadata, transcripts — to the Seagate Portable Drive. Takes 5-15 minutes depending on episode length.'
      ),
      h(
        'div',
        { class: 'text-body-sm text-ink-tertiary space-y-1.5' },
        bullet(
          `Source: /Volumes/1TB_SSD/cascade/episodes/${episodeId}/`
        ),
        bullet('Target: /Volumes/Seagate Portable Drive/podcast/'),
        bullet(
          `Duration: ${formatDuration(ep.duration_seconds as number)}`
        )
      )
    ),
    canBackup
      ? h(
          'div',
          { class: 'panel p-6 flex flex-col gap-3 border-status-warning/30' },
          h(
            'div',
            { class: 'flex items-center gap-2' },
            h('span', {
              class: 'w-2 h-2 rounded-full bg-status-warning',
            }),
            h(
              'span',
              { class: 'text-body text-status-warning font-medium' },
              `Type "${expectedPhrase}" to confirm`
            )
          ),
          h(
            'p',
            { class: 'text-body-sm text-ink-secondary' },
            'This also unlocks SD-card clearing. Cascade never clears an SD card without a verified backup.'
          ),
          confirmInput,
          approveBtn
        )
      : h(
          'div',
          { class: 'panel p-6' },
          h(
            'p',
            { class: 'text-body text-ink-secondary' },
            `Not ready to back up yet — current status: ${status.label}. ${status.hint}`
          )
        )
  );
}

function bullet(text: string): HTMLElement {
  return h(
    'div',
    { class: 'flex items-start gap-2' },
    h('span', { class: 'text-ink-tertiary mt-[3px]' }, '·'),
    h('span', { class: 'font-mono tabular text-ink-secondary' }, text)
  );
}
