import { Button } from '../components/Button';
import { Icon } from '../components/icons';
import { h, mount } from '../lib/dom';
import { api, type DeliveryStatus, type UnknownRecord } from '../lib/api';
import { pluralize } from '../lib/format';
import { link } from '../lib/router';
import { effect, onCleanup, signal } from '../lib/signals';
import { showToast } from '../state/ui';
import { stableControl, type StableControl } from '../lib/stable-control';
import {
  QualityReview,
  type QualityReviewControls,
} from '../components/QualityReview';
import type { QualitySnapshot } from '../lib/api';

interface DeliveryControls {
  video?: StableControl<HTMLVideoElement>;
  trimStart?: StableControl<HTMLInputElement>;
  trimEnd?: StableControl<HTMLInputElement>;
  quality: QualityReviewControls;
}

export function Delivery(target: HTMLElement, episodeId: string): void {
  const episode = signal<UnknownRecord | null>(null);
  const status = signal<DeliveryStatus | null>(null);
  const page = h('div', { class: 'min-h-full' });
  const controls: DeliveryControls = { quality: { previews: new Map() } };
  let pollTimer: number | undefined;
  let disposed = false;
  onCleanup(() => {
    disposed = true;
    window.clearTimeout(pollTimer);
  });

  const load = async (): Promise<void> => {
    try {
      const [ep, delivery] = await Promise.all([
        api.getEpisode(episodeId),
        api.deliveryStatus(episodeId),
      ]);
      if (disposed) return;
      episode.set(ep);
      status.set(delivery);
      if (delivery.video_status === 'preparing') {
        window.clearTimeout(pollTimer);
        pollTimer = window.setTimeout(load, 2000);
      }
    } catch (error) {
      showToast((error as Error).message, 'error');
    }
  };

  effect(() => {
    page.replaceChildren(renderPage(episodeId, episode(), status(), controls, async () => {
      try {
        const update = await api.prepareDeliveryVideo(episodeId);
        status.set({ ...(status.peek() ?? update), ...update });
        showToast('Upload video preparation started.', 'success');
        window.clearTimeout(pollTimer);
        pollTimer = window.setTimeout(load, 1000);
      } catch (error) {
        showToast((error as Error).message, 'error');
        await load();
      }
    }, async (start, end) => {
      try {
        status.set(await api.saveDeliveryTrim(episodeId, start, end));
        showToast('Episode trim saved. Prepare the video to apply it.', 'success');
      } catch (error) {
        showToast((error as Error).message, 'error');
      }
    }, load));
  });

  void load();
  mount(target, page);
}

function renderPage(
  episodeId: string,
  episode: UnknownRecord | null,
  delivery: DeliveryStatus | null,
  controls: DeliveryControls,
  prepareVideo: () => Promise<void>,
  saveTrim: (start: number, end: number) => Promise<void>,
  refresh: () => Promise<void>
): HTMLElement {
  const title = episode
    ? String(episode.episode_name || episode.title || episode.guest_name || episodeId)
    : 'Loading episode…';
  const loading = !delivery;
  const quality =
    delivery?.quality ??
    (episode?.quality as QualitySnapshot | null | undefined);

  return h(
    'div',
    { class: 'max-w-[820px] mx-auto px-10 py-12 flex flex-col gap-6' },
    h(
      'a',
      {
        ...link(`/episodes/${episodeId}`),
        class: 'text-body-sm text-ink-tertiary hover:text-ink-primary',
      },
      '← Back to episode'
    ),
    h('div', null,
      h('div', { class: 'text-heading-sm uppercase text-ink-tertiary mb-2' }, 'Release preparation'),
      h('h1', { class: 'font-display text-display-xl text-ink-primary' }, title),
      h(
        'p',
        { class: 'text-body text-ink-secondary mt-3 max-w-[640px]' },
        'Build and review the local release video, verify release quality, and download the finished file. This does not upload or publish anything.'
      )
    ),
    loading
      ? h('div', { class: 'panel p-6 animate-pulse-breath text-ink-tertiary' }, 'Loading release state…')
      : QualityReview({
          episodeId,
          quality,
          onUpdated: refresh,
          controls: controls.quality,
        }),
    clipReviewEntry(episodeId, episode, quality),
    delivery ? trimDetails(delivery, controls, saveTrim) : null,
    delivery ? videoDetails(delivery, controls, prepareVideo) : null
  );
}

function clipReviewEntry(
  episodeId: string,
  episode: UnknownRecord | null,
  quality?: QualitySnapshot | null
): HTMLElement | null {
  const renderedCount = quality?.artifacts.rendered_short_count ?? 0;
  const candidateCount = quality?.artifacts.candidate_count ??
    (Array.isArray(episode?.clips) ? episode.clips.length : 0);
  if (renderedCount === 0 && candidateCount === 0) return null;

  const firstClip = quality?.artifacts.rendered_short_ids[0];
  const path = firstClip
    ? `/episodes/${episodeId}/clips/review/${encodeURIComponent(firstClip)}`
    : `/episodes/${episodeId}/clips/review`;
  const count = renderedCount || candidateCount;
  const rendered = renderedCount > 0;
  const heading = rendered
    ? `${pluralize(count, 'rendered clip')} ready to watch`
    : `${pluralize(count, 'clip candidate')} ready to review`;
  const detail = rendered
    ? 'Play every short with sound, seeking, and fullscreen controls.'
    : 'Open editorial review to select, render, and inspect clips.';

  return h(
    'section',
    { class: 'panel p-6 flex items-center justify-between gap-5 flex-wrap border-accent/40' },
    h(
      'div',
      null,
      h('div', { class: 'text-heading-sm text-ink-primary' }, heading),
      h('p', { class: 'text-body-sm text-ink-tertiary mt-1' }, detail)
    ),
    h(
      'a',
      {
        ...link(path),
        class:
          'inline-flex h-11 px-5 items-center justify-center gap-2.5 rounded-md bg-accent text-ink-on-accent text-body-lg font-medium hover:brightness-110',
      },
      rendered ? Icon.play({ size: 18 }) : null,
      `${rendered ? 'Watch' : 'Review'} ${pluralize(count, 'clip')}`
    )
  );
}

function trimDetails(
  delivery: DeliveryStatus,
  controls: DeliveryControls,
  saveTrim: (start: number, end: number) => Promise<void>
): HTMLElement {
  const busy = delivery.video_status === 'preparing';
  controls.trimStart = stableControl(controls.trimStart, 'trim-start', () =>
    h('input', {
      value: formatTimestamp(delivery.trim_start_seconds ?? 0),
      class: 'w-full h-10 bg-surface-2 border border-border rounded-md px-3 text-body text-ink-primary focus:border-accent focus:outline-none',
      'aria-label': 'Episode start time',
    }) as HTMLInputElement
  );
  controls.trimEnd = stableControl(controls.trimEnd, 'trim-end', () =>
    h('input', {
      value: formatTimestamp(delivery.trim_end_seconds ?? delivery.source_duration_seconds ?? 0),
      class: 'w-full h-10 bg-surface-2 border border-border rounded-md px-3 text-body text-ink-primary focus:border-accent focus:outline-none',
      'aria-label': 'Episode end time',
    }) as HTMLInputElement
  );
  const startInput = controls.trimStart.value;
  const endInput = controls.trimEnd.value;
  return h(
    'div',
    { class: 'panel p-6 flex flex-col gap-4' },
    h('div', null,
      h('div', { class: 'text-heading-sm text-ink-primary' }, 'Episode range'),
      h('div', { class: 'text-body-sm text-ink-tertiary mt-1' },
        `Choose the conversation start and end within the ${formatDuration(delivery.source_duration_seconds)} source. Use seconds or HH:MM:SS.`
      )
    ),
    h('div', { class: 'grid grid-cols-2 gap-4' },
      h('label', { class: 'text-body-sm text-ink-secondary flex flex-col gap-1.5' }, 'Start', startInput),
      h('label', { class: 'text-body-sm text-ink-secondary flex flex-col gap-1.5' }, 'End', endInput)
    ),
    Button({
      variant: 'secondary',
      label: 'Save episode range',
      disabled: busy,
      onClick: () => {
        const start = parseTimestamp(startInput.value);
        const end = parseTimestamp(endInput.value);
        const duration = delivery.source_duration_seconds ?? 0;
        if (start == null || end == null || !(0 <= start && start < end && end <= duration)) {
          showToast(`Enter a range between 00:00:00 and ${formatTimestamp(duration)}.`, 'error');
          return;
        }
        void saveTrim(start, end);
      },
    })
  );
}

function videoDetails(
  delivery: DeliveryStatus,
  controls: DeliveryControls,
  prepareVideo: () => Promise<void>
): HTMLElement {
  const videoState = delivery.video_status ?? 'not_prepared';
  const busy = videoState === 'preparing';
  const progress = Math.min(99, Math.max(0, delivery.video_progress ?? 0));
  const video = delivery.video;
  return h(
    'div',
    { class: 'panel p-6 flex flex-col gap-5' },
    h('div', { class: 'flex items-center justify-between gap-4' },
      h('div', null,
        h('div', { class: 'text-heading-sm text-ink-primary' },
          videoState === 'ready' ? 'Rendered video available' : busy ? 'Preparing release video' : videoState === 'failed' ? 'Video preparation failed' : 'Release video'
        ),
        h('div', { class: 'text-body-sm text-ink-tertiary mt-1' },
          busy
            ? `${delivery.video_detail || 'Encoding'} · ${progress.toFixed(0)}%`
            : videoState === 'ready' && video?.render_mode === 'speaker_cut'
              ? 'Speaker-cut 1080p render with saved edits and mastered audio.'
              : videoState === 'ready'
                ? 'An earlier render is available. Prepare again to build the current speaker-cut 1080p video.'
                : 'Prepare a speaker-cut 1080p video with the saved edits and mastered audio.'
        )
      ),
      Button({
        variant: 'primary',
        size: 'lg',
        label: busy ? 'Preparing…' : videoState === 'ready' ? 'Prepare again' : 'Prepare video',
        loading: busy,
        disabled: busy,
        onClick: () => void prepareVideo(),
      })
    ),
    busy
      ? h('div', { class: 'h-2 rounded-full bg-surface-3 overflow-hidden' },
          h('div', {
            class: 'h-full bg-accent transition-all',
            style: { width: `${Math.max(1, progress)}%` },
          })
        )
      : null,
    delivery.video_error
      ? h('div', { class: 'rounded-md bg-status-danger/10 border border-status-danger/30 p-4 text-body text-status-danger' }, delivery.video_error)
      : null,
    videoState === 'ready' && video
      ? h('div', { class: 'border-t border-border pt-5 flex flex-col gap-4' },
          videoPlayer(delivery, controls),
          h('div', { class: 'grid grid-cols-2 gap-x-8 gap-y-3' },
            metric('Duration', formatDuration(video.duration_seconds)),
            metric('File size', formatBytes(video.size_bytes)),
            metric('Resolution', `${video.width}×${video.height}`),
            metric('Codecs', `${video.video_codec.toUpperCase()} / ${video.audio_codec.toUpperCase()}`),
            metric('Saved edits applied', String(video.edit_count))
          ),
          h('a', {
            href: delivery.video_download_url,
            download: video.filename,
            class: 'inline-flex h-11 px-5 items-center justify-center self-start rounded-md bg-accent text-ink-on-accent font-medium hover:brightness-110',
          }, 'Download rendered video')
        )
      : null
  );
}

function metric(label: string, value: string): HTMLElement {
  return h('div', null,
    h('div', { class: 'text-body-sm text-ink-tertiary' }, label),
    h('div', { class: 'text-body text-ink-primary font-medium' }, value)
  );
}

function videoPlayer(
  delivery: DeliveryStatus,
  controls: DeliveryControls
): HTMLVideoElement {
  const identity = `${delivery.video_download_url ?? ''}:${delivery.video_completed_at ?? ''}`;
  controls.video = stableControl(controls.video, identity, () =>
    h('video', {
      controls: true,
      preload: 'metadata',
      src: delivery.video_download_url,
      class: 'w-full rounded-md bg-black',
    }) as HTMLVideoElement
  );
  return controls.video.value;
}

function formatDuration(value?: number): string {
  if (value == null) return '—';
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const seconds = Math.floor(value % 60);
  return [hours, minutes, seconds].map((part) => String(part).padStart(2, '0')).join(':');
}

function formatBytes(value?: number): string {
  return value == null ? '—' : `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function parseTimestamp(value: string): number | null {
  const parts = value.trim().split(':');
  if (!parts.length || parts.length > 3) return null;
  const numbers = parts.map(Number);
  if (numbers.some((part) => !Number.isFinite(part) || part < 0)) return null;
  if (numbers.length === 1) return numbers[0];
  if (numbers.slice(1).some((part) => part >= 60)) return null;
  return numbers.reduce((total, part) => total * 60 + part, 0);
}

function formatTimestamp(value: number): string {
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const seconds = value % 60;
  return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${seconds.toFixed(3).padStart(6, '0')}`;
}
