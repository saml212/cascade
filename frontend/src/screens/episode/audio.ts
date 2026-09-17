import {
  QualityReview,
  type QualityReviewControls,
} from '../../components/QualityReview';
import {
  api,
  type DeliveryStatus,
  type QualitySnapshot,
} from '../../lib/api';
import { h } from '../../lib/dom';
import {
  formatDuration,
  formatOffsetMs,
  formatRelative,
} from '../../lib/format';
import { effect, onCleanup, signal } from '../../lib/signals';
import { stableControl, type StableControl } from '../../lib/stable-control';

interface AudioControls {
  master?: StableControl<HTMLAudioElement>;
  quality: QualityReviewControls;
}

export function renderAudio(
  target: HTMLElement,
  episode: Record<string, unknown>,
  episodeId: string
): void {
  const quality = signal<QualitySnapshot | null>(
    (episode.quality as QualitySnapshot | null | undefined) ?? null
  );
  const delivery = signal<DeliveryStatus | null>(
    (episode.delivery as DeliveryStatus | null | undefined) ?? null
  );
  const loadError = signal<string | null>(null);
  const controls: AudioControls = { quality: { previews: new Map() } };
  let disposed = false;
  onCleanup(() => {
    disposed = true;
  });

  const refresh = async (): Promise<void> => {
    try {
      const [nextQuality, nextDelivery] = await Promise.all([
        api.quality(episodeId),
        api.deliveryStatus(episodeId),
      ]);
      if (disposed) return;
      quality.set(nextQuality);
      delivery.set(nextDelivery);
      loadError.set(null);
    } catch (error) {
      if (!disposed) loadError.set((error as Error).message);
    }
  };

  effect(() => {
    const currentQuality = quality();
    const currentDelivery = delivery();
    const error = loadError();
    target.replaceChildren(
      h(
        'div',
        { class: 'flex flex-col gap-6' },
        error
          ? h(
              'div',
              {
                class:
                  'rounded-md border border-status-danger/30 bg-status-danger/10 px-4 py-3 text-body text-status-danger',
                role: 'alert',
              },
              `Could not refresh current audio state: ${error}`
            )
          : null,
        audioSourcePanel(episode, currentQuality),
        masterPanel(currentDelivery, controls),
        QualityReview({
          episodeId,
          quality: currentQuality,
          onUpdated: refresh,
          controls: controls.quality,
        }),
        routingPanel(episode)
      )
    );
  });

  void refresh();
}

function audioSourcePanel(
  episode: Record<string, unknown>,
  quality: QualitySnapshot | null
): HTMLElement {
  const inventory = audioInventory(episode);
  const selection = quality?.audio_quality.repair_selection;
  const selectedRepair =
    selection?.status && selection.status !== 'not_selected' ? selection : null;
  const [sourceLabel, sourceDetail] = selectedRepair
    ? repairDescription(selectedRepair)
    : baseSourceDescription(inventory);

  return h(
    'section',
    { class: 'panel p-6' },
    h(
      'div',
      { class: 'text-heading-sm uppercase text-ink-tertiary' },
      'Selected audio source'
    ),
    h(
      'div',
      { class: 'font-display text-display-md text-ink-primary mt-1' },
      sourceLabel
    ),
    h(
      'p',
      { class: 'text-body-sm text-ink-secondary mt-2 max-w-[720px]' },
      sourceDetail
    )
  );
}

function masterPanel(
  delivery: DeliveryStatus | null,
  controls: AudioControls
): HTMLElement {
  const selectedUrl = delivery?.selected_audio_download_url;
  const historicalUrl = delivery?.download_url;
  const provenance = delivery?.selected_audio?.provenance;
  const playable = Boolean(selectedUrl || historicalUrl);
  const title = selectedUrl
    ? provenance?.kind === 'selected_repair'
      ? 'Current selected repair master'
      : 'Available base mix · currentness unverified'
    : historicalUrl
      ? 'Historical podcast MP3'
      : 'Audio master unavailable';
  const detail = selectedUrl
    ? provenance?.kind === 'selected_repair'
      ? 'SOURCE CLOCK · This revision-validated selected repair is full length. Editorial cuts are not applied. Review the final rendered video for output timing and content.'
      : 'SOURCE CLOCK · This existing base mix is available for reference, but its currentness is unverified. Editorial cuts are not applied. Review the final rendered video for output timing and content.'
    : historicalUrl
      ? 'HISTORICAL OUTPUT · This read-only retired export may reflect an earlier delivery cut. Review the final rendered video for current output timing and content.'
      : delivery?.selected_audio_review_error ||
        'The selected or mixed full-length master has not been created yet.';

  return h(
    'section',
    { class: 'panel p-6 flex flex-col gap-4' },
    h(
      'div',
      { class: 'flex items-start justify-between gap-4 flex-wrap' },
      h(
        'div',
        null,
        h(
          'div',
          { class: 'text-heading-sm uppercase text-ink-tertiary' },
          'Full-length audio reference'
        ),
        h(
          'div',
          { class: 'font-display text-display-md text-ink-primary mt-1' },
          title
        ),
        h(
          'p',
          { class: 'text-body-sm text-ink-secondary mt-2 max-w-[680px]' },
          detail
        )
      )
    ),
    delivery && playable
      ? h(
          'div',
          { class: 'grid grid-cols-2 sm:grid-cols-4 gap-3' },
          measurement(
            'File',
            selectedUrl
              ? delivery.selected_audio?.filename || 'Selected audio'
              : delivery.filename || 'Historical MP3'
          ),
          measurement(
            'Size',
            formatBytes(
              selectedUrl ? delivery.selected_audio?.size_bytes : delivery.size_bytes
            )
          ),
          ...(!selectedUrl && historicalUrl
            ? [
                measurement('Duration', formatDuration(delivery.duration_seconds)),
                measurement(
                  'Integrated loudness',
                  numberUnit(delivery.integrated_lufs, ' LUFS')
                ),
              ]
            : [])
        )
      : null,
    delivery && playable ? masterPlayer(delivery, controls) : null,
    delivery?.completed_at && historicalUrl && !selectedUrl
      ? h(
          'div',
          { class: 'text-body-sm text-ink-tertiary' },
          `Historical file measured ${formatRelative(delivery.completed_at)}`
        )
      : null
  );
}

function masterPlayer(
  delivery: DeliveryStatus,
  controls: AudioControls
): HTMLAudioElement {
  const source = delivery.selected_audio_download_url || delivery.download_url;
  const identity = `${source ?? ''}:${delivery.completed_at ?? ''}`;
  controls.master = stableControl(controls.master, identity, () =>
    h('audio', {
      controls: true,
      preload: 'metadata',
      src: source,
      class: 'w-full',
      'aria-label': delivery.selected_audio_download_url
        ? delivery.selected_audio?.provenance.kind === 'selected_repair'
          ? 'Current selected repair master on the source clock'
          : 'Available base mix on the source clock with unverified currentness'
        : 'Historical podcast MP3',
    }) as HTMLAudioElement
  );
  return controls.master.value;
}

function formatBytes(value: number | undefined): string {
  return value == null ? '—' : `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function routingPanel(episode: Record<string, unknown>): HTMLElement {
  const inventory = audioInventory(episode);
  const sync = episode.audio_sync as Record<string, unknown> | undefined;
  const cropConfig = episode.crop_config as Record<string, unknown> | undefined;
  const speakers =
    (cropConfig?.speakers as Array<Record<string, unknown>> | undefined) ?? [];
  const ambient =
    (cropConfig?.ambient_tracks as Array<Record<string, unknown>> | undefined) ?? [];
  const offset = finiteNumber(sync?.offset_seconds);
  const confidence = finiteNumber(sync?.confidence);
  const drift = finiteNumber(sync?.drift_rate_ppm);

  return h(
    'section',
    { class: 'panel p-6 flex flex-col gap-4' },
    h(
      'div',
      null,
      h(
        'div',
        { class: 'text-heading-sm uppercase text-ink-tertiary' },
        'Input routing'
      ),
      h(
        'div',
        { class: 'text-body text-ink-secondary mt-1' },
        inventory.fileCount
          ? inventorySummary(inventory)
          : 'No extracted track inventory'
      )
    ),
    sync
      ? h(
          'div',
          { class: 'grid grid-cols-3 gap-4 rounded-md bg-surface-2 px-4 py-3' },
          measurement(
            'Sync offset',
            formatOffsetMs(offset)
          ),
          measurement(
            'Confidence',
            confidence == null ? '—' : `${Math.round(confidence * 100)}%`
          ),
          measurement(
            'Drift',
            drift == null ? '—' : `${drift.toFixed(1)} ppm`
          )
        )
      : h(
          'p',
          { class: 'text-body-sm text-ink-tertiary' },
          inventory.cameraChannels > 0 && inventory.recorderTracks === 0
            ? 'Camera audio is already on the video source clock.'
            : 'Recorder sync has not been recorded.'
        ),
    speakers.length
      ? h(
          'div',
          { class: 'divide-y divide-border-subtle' },
          ...speakers.map((speaker, index) =>
            h(
              'div',
              { class: 'flex items-center gap-3 py-2.5' },
              h('span', {
                class: 'w-2.5 h-2.5 rounded-full',
                style: { background: `var(--speaker-${(index % 4) + 1})` },
              }),
              h(
                'span',
                { class: 'text-body text-ink-primary flex-1' },
                String(speaker.label || `Speaker ${index + 1}`)
              ),
              h(
                'span',
                { class: 'text-code-sm text-ink-tertiary font-mono tabular' },
                speaker.track != null
                  ? `Recorder track ${speaker.track}`
                  : inventory.cameraChannels > 0 && inventory.recorderTracks === 0
                    ? 'Camera mix'
                    : 'No dedicated track'
              )
            )
          )
        )
      : h(
          'p',
          { class: 'text-body-sm text-ink-tertiary' },
          'No speaker-to-track assignments are available.'
        ),
    ambient.length
      ? h(
          'div',
          { class: 'border-t border-border-subtle pt-3' },
          h(
            'div',
            { class: 'text-heading-sm uppercase text-ink-tertiary mb-2' },
            'Ambient tracks'
          ),
          ...ambient.map((track) =>
            h(
              'div',
              { class: 'flex justify-between gap-4 py-1 text-body-sm' },
              h(
                'span',
                { class: 'text-ink-secondary' },
                String(track.stem || `Track ${track.track_number ?? '—'}`)
              ),
              h(
                'span',
                { class: 'font-mono tabular text-ink-tertiary' },
                `Volume ${numberUnit(track.volume, '')}`
              )
            )
          )
        )
      : null
  );
}

interface AudioInventory {
  cameraChannels: number;
  fileCount: number;
  recorderTracks: number;
}

function audioInventory(episode: Record<string, unknown>): AudioInventory {
  const tracks =
    (episode.audio_tracks as Array<Record<string, unknown>> | undefined) ?? [];
  const recorderTracks = new Set(
    tracks
      .filter((track) => track.track_type === 'input')
      .map((track) => finiteNumber(track.track_number))
      .filter((track): track is number => track != null)
  );
  return {
    cameraChannels: tracks.filter(
      (track) => track.track_type === 'camera_channel'
    ).length,
    fileCount: tracks.length,
    recorderTracks: recorderTracks.size,
  };
}

function baseSourceDescription(
  inventory: AudioInventory
): [label: string, detail: string] {
  if (inventory.recorderTracks) {
    return [
      'External recorder mix',
      `${inventory.recorderTracks} logical recorder ${inventory.recorderTracks === 1 ? 'track is' : 'tracks are'} available across ${inventory.fileCount} extracted ${inventory.fileCount === 1 ? 'file' : 'files'}.`,
    ];
  }
  if (inventory.cameraChannels) {
    return [
      'Camera-channel mix',
      `${inventory.cameraChannels} preserved camera ${inventory.cameraChannels === 1 ? 'channel feeds' : 'channels feed'} the canonical mix.`,
    ];
  }
  return [
    'Audio source unavailable',
    'No canonical source description is available yet.',
  ];
}

function repairDescription(
  selection: NonNullable<
    QualitySnapshot['audio_quality']['repair_selection']
  >
): [label: string, detail: string] {
  if (selection.status === 'stale') {
    return [
      'Grounded repair selected · stale',
      `The selected repair is stale: ${selection.detail ?? 'its inputs changed'}.`,
    ];
  }
  return [
    'Grounded repair selected',
    selection.release_safe === false
      ? 'The repair is selected for future renders, while the current release still requires review.'
      : 'Future renders use the revision-bound repair selection.',
  ];
}

function inventorySummary(inventory: AudioInventory): string {
  const parts: string[] = [];
  if (inventory.recorderTracks) {
    parts.push(
      `${inventory.recorderTracks} logical recorder ${inventory.recorderTracks === 1 ? 'track' : 'tracks'}`
    );
  }
  if (inventory.cameraChannels) {
    parts.push(
      `${inventory.cameraChannels} camera ${inventory.cameraChannels === 1 ? 'channel' : 'channels'}`
    );
  }
  const sources = parts.length ? parts.join(' and ') : 'auxiliary audio';
  return `${sources} across ${inventory.fileCount} extracted ${inventory.fileCount === 1 ? 'file' : 'files'}`;
}

function measurement(label: string, value: string): HTMLElement {
  return h(
    'div',
    null,
    h(
      'div',
      { class: 'text-heading-sm uppercase text-ink-tertiary' },
      label
    ),
    h(
      'div',
      { class: 'text-body text-ink-primary font-mono tabular mt-1' },
      value
    )
  );
}

function finiteNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function numberUnit(value: unknown, unit: string): string {
  const number = finiteNumber(value);
  return number == null ? '—' : `${number.toFixed(1)}${unit}`;
}
