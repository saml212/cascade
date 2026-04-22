import { h, mount } from '../../lib/dom';
import { effect } from '../../lib/signals';
import { link, navigate, currentPath } from '../../lib/router';
import { describeAgent, describeStatus, episodeTitle } from '../../lib/format';
import { StatusPill } from '../../components/StatusPill';
import { Button } from '../../components/Button';
import { StepProgress } from '../../components/StepProgress';
import { Icon } from '../../components/icons';
import {
  episodeDetail,
  episodeDetailError,
  watchEpisode,
} from '../../state/episodes';
import { renderOverview } from './overview';
import { renderLongform } from './longform';
import { renderClips } from './clips';
import { renderAudio } from './audio';
import { renderMetadata } from './metadata';

type SectionKey = 'overview' | 'longform' | 'clips' | 'audio' | 'metadata';

const SECTIONS: Array<{ key: SectionKey; label: string }> = [
  { key: 'overview', label: 'Overview' },
  { key: 'longform', label: 'Longform' },
  { key: 'clips', label: 'Clips' },
  { key: 'audio', label: 'Audio' },
  { key: 'metadata', label: 'Metadata' },
];

function sectionFromPath(path: string, id: string): SectionKey {
  const base = `/episodes/${id}`;
  if (path === `${base}/longform`) return 'longform';
  if (path === `${base}/clips`) return 'clips';
  if (path === `${base}/audio`) return 'audio';
  if (path === `${base}/metadata`) return 'metadata';
  return 'overview';
}

export function Episode(target: HTMLElement, episodeId: string): void {
  watchEpisode(episodeId);

  const content = h('div');

  const header = h('header', {
    class: 'px-10 pt-8 pb-6 border-b border-border-subtle sticky top-0 bg-canvas/90 backdrop-blur-sm z-10',
  });

  const page = h(
    'div',
    { class: 'min-h-full' },
    header,
    h('div', { class: 'px-10 py-8 max-w-[1280px] mx-auto' }, content)
  );

  effect(() => {
    const ep = episodeDetail();
    const err = episodeDetailError();

    if (err && !ep) {
      header.replaceChildren(errorHeader(err));
      content.replaceChildren();
      return;
    }

    if (!ep) {
      header.replaceChildren(loadingHeader());
      content.replaceChildren(loadingBody());
      return;
    }

    header.replaceChildren(renderHeader(ep, episodeId));
    const section = sectionFromPath(currentPath(), episodeId);
    renderSection(content, ep, episodeId, section);
  });

  mount(target, page);
}

function renderSection(
  target: HTMLElement,
  ep: Record<string, unknown>,
  episodeId: string,
  section: SectionKey
): void {
  switch (section) {
    case 'longform':
      renderLongform(target, ep, episodeId);
      break;
    case 'clips':
      renderClips(target, ep, episodeId);
      break;
    case 'audio':
      renderAudio(target, ep, episodeId);
      break;
    case 'metadata':
      renderMetadata(target, ep, episodeId);
      break;
    default:
      renderOverview(target, ep, episodeId);
  }
}

function renderHeader(
  ep: Record<string, unknown>,
  episodeId: string
): HTMLElement {
  const status = describeStatus(ep.status as string, {
    cropConfig: ep.crop_config,
    clips: ep.clips as unknown[] | undefined,
  });
  const title = episodeTitle(ep, episodeId);
  const subtitle = (ep.guest_title as string) || (ep.episode_name as string) || '';
  const pipeline = ep.pipeline as Record<string, unknown> | undefined;
  const currentAgent = (pipeline?.current_agent as string) ?? null;
  const completed = (pipeline?.agents_completed as string[]) ?? [];
  const requested = (pipeline?.agents_requested as string[]) ?? [];
  const agents = requested.length > 0 ? requested : completed;
  const errors = (pipeline?.errors as Record<string, string>) ?? {};
  const erroredList = Object.keys(errors);

  const path = currentPath();
  const activeSection = sectionFromPath(path, episodeId);

  const tabs = h(
    'nav',
    { class: 'flex gap-1 mt-6 -mb-6 border-b border-border-subtle' },
    ...SECTIONS.map((s) => {
      const active = s.key === activeSection;
      const target =
        s.key === 'overview'
          ? `/episodes/${episodeId}`
          : `/episodes/${episodeId}/${s.key}`;
      return h(
        'a',
        {
          ...link(target),
          class: [
            'px-4 py-3 text-body font-medium border-b-2 transition-colors duration-[120ms]',
            active
              ? 'border-accent text-ink-primary'
              : 'border-transparent text-ink-secondary hover:text-ink-primary',
          ].join(' '),
        },
        s.label
      );
    })
  );

  const isProcessing = status.key === 'processing';

  return h(
    'div',
    { class: 'max-w-[1280px] mx-auto' },
    h(
      'div',
      { class: 'flex items-start gap-6 mb-3' },
      h(
        'a',
        {
          ...link('/'),
          class:
            'shrink-0 w-8 h-8 rounded-md flex items-center justify-center text-ink-tertiary hover:text-ink-primary hover:bg-surface-2 mt-1',
          title: 'Back to dashboard',
        },
        Icon.chevronLeft()
      ),
      h(
        'div',
        { class: 'flex-1 min-w-0' },
        h(
          'div',
          { class: 'flex items-center gap-3 flex-wrap' },
          h(
            'span',
            {
              class: 'text-code text-ink-tertiary font-mono tabular',
            },
            episodeId
          ),
          StatusPill({ descriptor: status, size: 'sm' })
        ),
        h(
          'h1',
          { class: 'font-display text-display-lg text-ink-primary mt-2 truncate' },
          title
        ),
        subtitle
          ? h(
              'p',
              { class: 'text-body-lg text-ink-secondary mt-1 truncate' },
              subtitle
            )
          : null
      ),
      primaryActionFor(status.key, episodeId)
    ),
    isProcessing && agents.length > 0
      ? h(
          'div',
          { class: 'mt-4 flex flex-col gap-2' },
          StepProgress({
            agents,
            completed,
            current: currentAgent,
            errored: erroredList,
          }),
          h(
            'div',
            {
              class:
                'flex items-baseline justify-between text-body-sm text-ink-secondary',
            },
            h('span', null, currentAgent ? describeAgent(currentAgent) : 'Queued'),
            h(
              'span',
              { class: 'font-mono tabular text-ink-tertiary' },
              `${completed.length} / ${agents.length} complete`
            )
          )
        )
      : null,
    tabs
  );
}

function primaryActionFor(key: string, episodeId: string): HTMLElement | null {
  switch (key) {
    case 'awaiting_crop':
      return Button({
        variant: 'primary',
        label: 'Set up crops',
        onClick: () => navigate(`/episodes/${episodeId}/crop-setup`),
      });
    case 'awaiting_longform_review':
      return Button({
        variant: 'primary',
        label: 'Review longform',
        onClick: () => navigate(`/episodes/${episodeId}/longform/review`),
      });
    case 'awaiting_clip_review':
      return Button({
        variant: 'primary',
        label: 'Review clips',
        onClick: () => navigate(`/episodes/${episodeId}/clips/review`),
      });
    case 'awaiting_publish':
      return Button({
        variant: 'primary',
        label: 'Publish',
        onClick: () => navigate(`/episodes/${episodeId}/publish`),
      });
    case 'awaiting_backup':
      return Button({
        variant: 'primary',
        label: 'Approve backup',
        onClick: () => navigate(`/episodes/${episodeId}/backup`),
      });
    default:
      return null;
  }
}

function loadingHeader(): HTMLElement {
  return h(
    'div',
    { class: 'max-w-[1280px] mx-auto' },
    h('div', {
      class: 'h-6 w-40 bg-surface-2 rounded-md animate-pulse-breath',
    }),
    h('div', {
      class: 'h-9 w-96 bg-surface-2 rounded-md mt-3 animate-pulse-breath',
    })
  );
}

function loadingBody(): HTMLElement {
  return h(
    'div',
    { class: 'grid grid-cols-3 gap-5' },
    ...Array.from({ length: 3 }).map(() =>
      h('div', {
        class: 'panel h-40 animate-pulse-breath',
      })
    )
  );
}

function errorHeader(err: string): HTMLElement {
  return h(
    'div',
    { class: 'max-w-[1280px] mx-auto' },
    h(
      'div',
      {
        class:
          'text-body text-status-danger bg-status-danger/10 border border-status-danger/30 rounded-lg px-5 py-4',
      },
      err
    )
  );
}

