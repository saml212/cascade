import { Button } from '../../components/Button';
import { QualityReview, type QualityReviewControls } from '../../components/QualityReview';
import { StatusPill } from '../../components/StatusPill';
import { StepProgress } from '../../components/StepProgress';
import { Icon } from '../../components/icons';
import { api, type DeliveryStatus, type EpisodeReviewState, type QualitySnapshot, type UnknownRecord } from '../../lib/api';
import { clipDistributionReady, publicationEvidenceStatusLabel } from '../../lib/clip-review-surface';
import { h, mount } from '../../lib/dom';
import {
  BACKUP_ARTIFACTS,
  BACKUP_TARGET_PATH,
  backupConfirmationMatches,
  episodeScheduleProjection,
  episodeSectionFromPath,
  episodeSectionPath,
  formatEpisodeTimestamp,
  parseEpisodeTimestamp,
  type EpisodePublicationEvidence,
  type EpisodeScheduleItem,
  type EpisodeSection,
} from '../../lib/episode-release';
import {
  describeAgent,
  describeEpisodeStatus,
  describeStatus,
  episodeDisplayDuration,
  episodeTitle,
  formatDuration,
  formatRelative,
  isVideoPreparationActive,
  pluralize,
} from '../../lib/format';
import { acceptSavedPatch, buildMetadataPatch, metadataValues, type MetadataField, type MetadataValues } from '../../lib/metadata-draft';
import { currentPath, link, navigate } from '../../lib/router';
import { effect, onCleanup, signal, type Signal } from '../../lib/signals';
import { stableControl, type StableControl } from '../../lib/stable-control';
import { episodeDetail, episodeDetailError } from '../../state/episodes';
import { showToast } from '../../state/ui';

type ProjectionKey = 'review' | 'quality' | 'delivery' | 'schedule';
type ProjectionErrors = Partial<Record<ProjectionKey, string>>;

interface SurfaceControls {
  audio?: StableControl<HTMLAudioElement>;
  video?: StableControl<HTMLVideoElement>;
  trimStart?: StableControl<HTMLInputElement>;
  trimEnd?: StableControl<HTMLInputElement>;
  quality: QualityReviewControls;
}

interface DraftState extends MetadataValues {
  saving: boolean;
  dirty: boolean;
  revision: number;
}

interface SurfaceContext {
  episodeId: string;
  review: Signal<EpisodeReviewState | null>;
  quality: Signal<QualitySnapshot | null>;
  delivery: Signal<DeliveryStatus | null>;
  schedule: Signal<UnknownRecord | null>;
  errors: Signal<ProjectionErrors>;
  controls: SurfaceControls;
  refreshAll: () => Promise<void>;
  prepareVideo: () => Promise<void>;
  saveTrim: (start: number, end: number) => Promise<void>;
}

const SECTION_NAV: Array<{ key: EpisodeSection; label: string }> = [
  { key: 'review', label: 'Review' },
  { key: 'audio', label: 'Audio' },
  { key: 'delivery', label: 'Release files' },
  { key: 'metadata', label: 'Episode copy' },
  { key: 'publication', label: 'Publication' },
  { key: 'backup', label: 'Backup' },
];

export function Episode(target: HTMLElement, episodeId: string): void {
  const review = signal<EpisodeReviewState | null>(null);
  const quality = signal<QualitySnapshot | null>(null);
  const delivery = signal<DeliveryStatus | null>(null);
  const schedule = signal<UnknownRecord | null>(null);
  const errors = signal<ProjectionErrors>({});
  const controls: SurfaceControls = { quality: { previews: new Map() } };
  const surfaceReady = signal(false);
  const sections = new Map<EpisodeSection, HTMLElement>();
  let deliveryTimer: number | undefined;
  let deliveryRequestPending = false;
  let disposed = false;
  let initialized = false;

  onCleanup(() => {
    disposed = true;
    window.clearTimeout(deliveryTimer);
  });

  function updateError(key: ProjectionKey, message?: string): void {
    const next = { ...errors.peek() };
    if (message) next[key] = message;
    else delete next[key];
    errors.set(next);
  }

  function acceptDelivery(next: DeliveryStatus): void {
    delivery.set(next);
    window.clearTimeout(deliveryTimer);
    if (next.video_status === 'preparing') {
      deliveryTimer = window.setTimeout(() => void refreshDelivery(), 2000);
    }
  }

  async function refreshDelivery(): Promise<void> {
    if (deliveryRequestPending || disposed) return;
    deliveryRequestPending = true;
    try {
      const next = await api.deliveryStatus(episodeId);
      if (disposed) return;
      acceptDelivery(next);
      updateError('delivery');
    } catch (error) {
      if (!disposed) updateError('delivery', (error as Error).message);
    } finally {
      deliveryRequestPending = false;
    }
  }

  async function refreshAll(): Promise<void> {
    const results = await Promise.allSettled([api.review(episodeId), api.quality(episodeId), api.deliveryStatus(episodeId), api.schedule()] as const);
    if (disposed) return;

    const [reviewResult, qualityResult, deliveryResult, scheduleResult] = results;
    if (reviewResult.status === 'fulfilled') {
      review.set(reviewResult.value);
      updateError('review');
    } else updateError('review', errorMessage(reviewResult.reason));
    if (qualityResult.status === 'fulfilled') {
      quality.set(qualityResult.value);
      updateError('quality');
    } else updateError('quality', errorMessage(qualityResult.reason));
    if (deliveryResult.status === 'fulfilled') {
      acceptDelivery(deliveryResult.value);
      updateError('delivery');
    } else updateError('delivery', errorMessage(deliveryResult.reason));
    if (scheduleResult.status === 'fulfilled') {
      schedule.set(scheduleResult.value);
      updateError('schedule');
    } else updateError('schedule', errorMessage(scheduleResult.reason));
  }

  async function prepareVideo(): Promise<void> {
    try {
      acceptDelivery(await api.prepareDeliveryVideo(episodeId));
      updateError('delivery');
      showToast('Upload video preparation started.', 'success');
    } catch (error) {
      const message = (error as Error).message;
      updateError('delivery', message);
      showToast(message, 'error');
      await refreshDelivery();
    }
  }

  async function saveTrim(start: number, end: number): Promise<void> {
    try {
      acceptDelivery(await api.saveDeliveryTrim(episodeId, start, end));
      updateError('delivery');
      showToast('Episode trim saved. Prepare the video to apply it.', 'success');
    } catch (error) {
      const message = (error as Error).message;
      updateError('delivery', message);
      showToast(message, 'error');
    }
  }

  const context: SurfaceContext = {
    episodeId,
    review,
    quality,
    delivery,
    schedule,
    errors,
    controls,
    refreshAll,
    prepareVideo,
    saveTrim,
  };
  const header = h('header', {
    class: 'px-4 sm:px-6 lg:px-10 pt-6 pb-4 border-b border-border-subtle sticky top-0 bg-canvas/95 backdrop-blur-sm z-20',
  });
  const content = h('div', {
    class: 'px-4 sm:px-6 lg:px-10 py-8 max-w-[1280px] mx-auto',
  });
  const page = h('div', { class: 'min-h-full' }, header, content);

  effect(() => {
    const episode = episodeDetail();
    const error = episodeDetailError();
    header.replaceChildren(episode ? renderHeader(episode, context) : error ? errorHeader(error) : loadingHeader());
  });

  effect(() => {
    const episode = episodeDetail();
    const error = episodeDetailError();
    if (!episode && !initialized) {
      content.replaceChildren(error ? inlineError(error) : loadingBody());
      return;
    }
    if (!episode || initialized) return;
    initialized = true;
    const surface = renderSurface(episode, context);
    for (const [key, currentSection] of surface.sections) {
      sections.set(key, currentSection);
    }
    content.replaceChildren(surface.root);
    surfaceReady.set(true);
  });

  effect(() => {
    if (!surfaceReady()) return;
    const currentSection = sections.get(episodeSectionFromPath(currentPath(), episodeId));
    if (!currentSection) return;
    if (currentSection instanceof HTMLDetailsElement) currentSection.open = true;
    queueMicrotask(() => {
      currentSection.scrollIntoView({ block: 'start' });
      currentSection.focus({ preventScroll: true });
    });
  });

  void refreshAll();
  mount(target, page);
}

function renderSurface(initialEpisode: UnknownRecord, context: SurfaceContext): { root: HTMLElement; sections: Map<EpisodeSection, HTMLElement> } {
  const sections = new Map<EpisodeSection, HTMLElement>();
  const projectionErrors = h('div');
  const reviewSection = section('review');
  const audioSection = section('audio');
  const deliverySection = section('delivery');
  const metadataSection = detailsSection(
    'metadata',
    'Episode copy',
    'Edit the saved episode and platform copy.',
    createMetadataEditor(initialEpisode, context.episodeId),
  );
  const publicationSection = detailsSection(
    'publication',
    'Publication approval',
    'Review exact release evidence and record approval without starting publication.',
    h('div'),
  );
  const backupSection = detailsSection('backup', 'Backup', 'Review the complete archive inventory before starting the backup agent.', h('div'));

  for (const element of [reviewSection, audioSection, deliverySection, metadataSection, publicationSection, backupSection]) {
    sections.set(element.dataset.section as EpisodeSection, element);
  }

  effect(() => {
    const currentErrors = context.errors();
    projectionErrors.replaceChildren(
      ...Object.entries(currentErrors).map(([key, message]) => inlineError(`${projectionLabel(key as ProjectionKey)}: ${message}`)),
    );
  });

  effect(() => {
    const episode = episodeDetail();
    if (!episode) return;
    reviewSection.replaceChildren(renderReview(episode, context.review(), context.quality(), context.schedule(), context));
  });

  effect(() => {
    const episode = episodeDetail();
    if (!episode) return;
    audioSection.replaceChildren(renderAudio(episode, context.quality(), context.delivery(), context.controls));
  });

  effect(() => {
    deliverySection.replaceChildren(renderDelivery(context));
  });

  const publicationBody = publicationSection.lastElementChild as HTMLElement;
  const approvalBusy = signal(false);
  const approvalError = signal<string | null>(null);
  effect(() => {
    publicationBody.replaceChildren(renderPublication(context, approvalBusy, approvalError));
  });

  const backupBody = backupSection.lastElementChild as HTMLElement;
  const backupBusy = signal(false);
  const backupPhrase = signal('');
  const backupError = signal<string | null>(null);
  const confirmationInput = h('input', {
    type: 'text',
    placeholder: 'Type "back it up" to enable the button',
    class:
      'w-full h-11 bg-surface-2 border border-border rounded-md px-4 text-body text-ink-primary placeholder:text-ink-disabled focus:border-accent focus:outline-none',
    oninput: (event: Event) => backupPhrase.set((event.target as HTMLInputElement).value),
  });
  effect(() => {
    const episode = episodeDetail();
    if (!episode) return;
    backupBody.replaceChildren(renderBackup(episode, context.episodeId, backupBusy, backupPhrase, backupError, confirmationInput));
  });

  return {
    root: h(
      'div',
      { class: 'flex flex-col gap-7 pb-20' },
      projectionErrors,
      reviewSection,
      audioSection,
      deliverySection,
      metadataSection,
      publicationSection,
      backupSection,
    ),
    sections,
  };
}

function renderHeader(episode: UnknownRecord, context: SurfaceContext): HTMLElement {
  const statusContext = {
    cropConfig: episode.crop_config,
    clips: context.review()?.clips ?? (episode.clips as unknown[] | undefined),
  };
  const rawStatus = describeStatus(episode.status as string, statusContext);
  const status = describeEpisodeStatus(episode, statusContext);
  const pipeline = episode.pipeline as Record<string, unknown> | undefined;
  const completed = (pipeline?.agents_completed as string[]) ?? [];
  const requested = (pipeline?.agents_requested as string[]) ?? [];
  const agents = requested.length ? requested : completed;
  const errors = (pipeline?.errors as Record<string, string>) ?? {};
  const currentAgent = (pipeline?.current_agent as string) ?? null;
  const active = episodeSectionFromPath(currentPath(), context.episodeId);

  return h(
    'div',
    { class: 'max-w-[1280px] mx-auto' },
    h(
      'div',
      { class: 'flex items-start gap-4 sm:gap-6' },
      h(
        'a',
        {
          ...link('/'),
          class: 'shrink-0 w-8 h-8 rounded-md flex items-center justify-center text-ink-tertiary hover:text-ink-primary hover:bg-surface-2 mt-1',
          title: 'Back to dashboard',
        },
        Icon.chevronLeft(),
      ),
      h(
        'div',
        { class: 'flex-1 min-w-0' },
        h(
          'div',
          { class: 'flex items-center gap-3 flex-wrap' },
          h('span', { class: 'text-code text-ink-tertiary font-mono tabular' }, context.episodeId),
          StatusPill({ descriptor: status, size: 'sm' }),
        ),
        h(
          'h1',
          {
            class: 'font-display text-display-lg text-ink-primary mt-2 break-words',
          },
          episodeTitle(episode, context.episodeId),
        ),
        h('p', { class: 'text-body text-ink-secondary mt-1' }, 'Episode review and release'),
      ),
      primaryAction(status.key, context.episodeId, context.delivery()),
    ),
    rawStatus.key === 'processing' && agents.length
      ? h(
          'div',
          { class: 'mt-4 flex flex-col gap-2' },
          StepProgress({
            agents,
            completed,
            current: currentAgent,
            errored: Object.keys(errors),
          }),
          h(
            'div',
            {
              class: 'flex justify-between text-body-sm text-ink-secondary',
            },
            h('span', null, currentAgent ? describeAgent(currentAgent) : 'Queued'),
            h('span', { class: 'font-mono tabular text-ink-tertiary' }, `${completed.length} / ${agents.length} complete`),
          ),
        )
      : null,
    h(
      'nav',
      {
        class: 'flex gap-1 mt-5 -mb-4 overflow-x-auto border-b border-border-subtle',
        'aria-label': 'Episode sections',
      },
      ...SECTION_NAV.map(({ key, label }) =>
        h(
          'a',
          {
            ...link(episodeSectionPath(context.episodeId, key)),
            class: [
              'shrink-0 px-3 py-3 text-body-sm font-medium border-b-2 transition-colors duration-[120ms]',
              active === key ? 'border-accent text-ink-primary' : 'border-transparent text-ink-secondary hover:text-ink-primary',
            ].join(' '),
          },
          label,
        ),
      ),
    ),
  );
}

function primaryAction(status: string, episodeId: string, delivery: DeliveryStatus | null): HTMLElement | null {
  if (isVideoPreparationActive(delivery)) {
    return Button({
      variant: 'primary',
      label: 'View preparation',
      onClick: () => navigate(episodeSectionPath(episodeId, 'delivery')),
    });
  }
  if (status === 'awaiting_crop') {
    return Button({
      variant: 'primary',
      label: 'Set up source',
      onClick: () => navigate(`/episodes/${encodeURIComponent(episodeId)}/crop-setup`),
    });
  }
  if (status === 'awaiting_longform_review') {
    return Button({
      variant: 'primary',
      label: 'Review longform',
      onClick: () => navigate(`/episodes/${encodeURIComponent(episodeId)}/longform/review`),
    });
  }
  if (status === 'awaiting_clip_review') {
    return Button({
      variant: 'primary',
      label: 'Review clips',
      onClick: () => navigate(`/episodes/${encodeURIComponent(episodeId)}/clips/review`),
    });
  }
  if (status === 'awaiting_publish') {
    return Button({
      variant: 'primary',
      label: 'Review approval',
      onClick: () => navigate(episodeSectionPath(episodeId, 'publication')),
    });
  }
  if (status === 'awaiting_backup') {
    return Button({
      variant: 'primary',
      label: 'Review backup',
      onClick: () => navigate(episodeSectionPath(episodeId, 'backup')),
    });
  }
  if (status === 'quality_blocked' || status === 'quality_review_required' || status === 'ready_to_render' || status === 'delivery_ready') {
    return Button({
      variant: 'primary',
      label: status === 'delivery_ready' ? 'Open release files' : 'Review release',
      onClick: () => navigate(episodeSectionPath(episodeId, 'delivery')),
    });
  }
  return null;
}

function renderReview(
  episode: UnknownRecord,
  review: EpisodeReviewState | null,
  quality: QualitySnapshot | null,
  schedule: UnknownRecord | null,
  context: SurfaceContext,
): HTMLElement {
  const status = describeEpisodeStatus(episode, {
    cropConfig: episode.crop_config,
    clips: review?.clips ?? (episode.clips as unknown[] | undefined),
  });
  const pipeline = episode.pipeline as Record<string, unknown> | undefined;
  const completed = (pipeline?.agents_completed as string[]) ?? [];
  const errors = (pipeline?.errors as Record<string, string>) ?? {};
  const retry = shouldOfferPreparationRetry(episode, pipeline, completed, errors);

  return h(
    'div',
    { class: 'flex flex-col gap-6' },
    h(
      'div',
      { class: 'grid grid-cols-1 xl:grid-cols-[1.25fr_1fr] gap-6' },
      h(
        'div',
        { class: 'panel p-6' },
        h('div', { class: 'text-heading-sm uppercase text-ink-tertiary mb-2' }, 'Where we are'),
        h('div', { class: 'font-display text-display-md text-ink-primary' }, status.hint || status.label),
        pipeline?.current_agent
          ? h(
              'p',
              { class: 'text-body text-ink-secondary mt-3' },
              'Right now: ',
              h('span', { class: 'text-ink-primary font-medium' }, describeAgent(String(pipeline.current_agent))),
            )
          : null,
        retry
          ? h(
              'div',
              { class: 'mt-5 flex flex-col items-start gap-2' },
              Button({
                variant: 'secondary',
                label: 'Retry preparation',
                onClick: async () => {
                  try {
                    await api.runPipeline(context.episodeId, {
                      agents: ['ingest', 'stitch', 'audio_analysis'],
                    });
                    showToast('Preparation restarted safely.', 'success');
                  } catch (error) {
                    showToast(`Could not restart preparation: ${(error as Error).message}`, 'error');
                  }
                },
              }),
              h('p', { class: 'text-body-sm text-ink-tertiary' }, 'Retries source import and analysis only. It will not render or publish.'),
            )
          : null,
        h(
          'div',
          {
            class: 'grid grid-cols-2 sm:grid-cols-4 gap-4 mt-6 pt-5 border-t border-border-subtle',
          },
          metric('Duration', formatDuration(episodeDisplayDuration(episode))),
          metric('Created', formatRelative(episode.created_at as string)),
          metric('Speakers', speakerCount(episode)),
          metric('Candidates', String(review?.clip_summary.candidate_count ?? ((episode.clips as unknown[]) ?? []).length)),
        ),
      ),
      h(
        'div',
        { class: 'panel p-5 flex flex-col gap-3' },
        h('div', { class: 'text-heading-sm uppercase text-ink-tertiary' }, 'Specialist review'),
        actionLink(`/episodes/${encodeURIComponent(context.episodeId)}/crop-setup`, 'Source setup', 'Crop geometry, recorder routing, and sync'),
        actionLink(`/episodes/${encodeURIComponent(context.episodeId)}/longform/review`, 'Longform review', 'Source-clock cuts and current render approval'),
        actionLink(
          `/episodes/${encodeURIComponent(context.episodeId)}/clips/review`,
          'Clip review',
          'Selections, variants, captions, and distribution approval',
        ),
      ),
    ),
    renderReleaseFacts(review, quality, schedule, context.episodeId),
  );
}

function renderReleaseFacts(
  review: EpisodeReviewState | null,
  quality: QualitySnapshot | null,
  schedule: UnknownRecord | null,
  episodeId: string,
): HTMLElement {
  const canonical = review?.longform.canonical_render;
  const approval = review?.longform.approval;
  const selected = review?.clips.filter((clip) => clip.review.selection.status === 'selected') ?? [];
  const ready = selected.filter((clip) => clipDistributionReady(clip.review));
  const scheduleState = episodeScheduleProjection(schedule, episodeId);
  const currentLongform = canonical?.current === true;
  const approvedLongform = currentLongform && approval?.current === true;
  const providerCount = scheduleState.publicationEvidence.length;
  const queueCount = scheduleState.items.length;

  return h(
    'section',
    { class: 'panel p-6' },
    h(
      'div',
      {
        class: 'flex items-baseline justify-between gap-4 flex-wrap mb-4',
      },
      h('div', { class: 'text-heading-md text-ink-primary' }, 'Release facts'),
      quality?.release_gate.revision
        ? h('div', { class: 'text-code-sm text-ink-tertiary font-mono break-all' }, `Revision ${quality.release_gate.revision}`)
        : null,
    ),
    h(
      'div',
      { class: 'grid grid-cols-1 lg:grid-cols-3 gap-4' },
      fact(
        'Longform proof',
        approvedLongform
          ? 'Current render approved'
          : currentLongform
            ? 'Current render needs approval'
            : canonical?.playable
              ? 'Previous render only'
              : 'Current render unavailable',
        canonical?.detail || 'Read from the canonical episode review projection.',
        approvedLongform ? 'success' : 'warning',
      ),
      fact(
        'Short review readiness',
        `${ready.length} of ${selected.length} selected ready`,
        selected.length ? 'Readiness requires the exact chosen distribution version and its current approval.' : 'No clips are selected for distribution.',
        selected.length > 0 && ready.length === selected.length ? 'success' : 'warning',
      ),
      fact(
        'Queue and publication evidence',
        `${pluralize(queueCount, 'schedule item')} · ${pluralize(providerCount, 'record')}`,
        providerCount || queueCount
          ? 'Exact provider states and URLs come from Schedule.'
          : 'No queue or provider publication evidence is recorded in Schedule.',
        providerCount || queueCount ? 'neutral' : 'warning',
      ),
    ),
  );
}

function renderAudio(episode: UnknownRecord, quality: QualitySnapshot | null, delivery: DeliveryStatus | null, controls: SurfaceControls): HTMLElement {
  const inventory = audioInventory(episode);
  const selection = quality?.audio_quality.repair_selection;
  const selectedRepair = selection?.status && selection.status !== 'not_selected' ? selection : null;
  const [sourceLabel, sourceDetail] = selectedRepair ? repairDescription(selectedRepair) : baseSourceDescription(inventory);
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
      ? 'SOURCE CLOCK · Revision-validated selected repair at full length. Editorial cuts are not applied.'
      : 'SOURCE CLOCK · Existing base mix with unverified currentness. Editorial cuts are not applied.'
    : historicalUrl
      ? 'HISTORICAL OUTPUT · This retired export may reflect an earlier delivery cut.'
      : delivery?.selected_audio_review_error || 'The selected or mixed full-length master has not been created yet.';

  return h(
    'section',
    { class: 'flex flex-col gap-4' },
    h(
      'div',
      null,
      h('div', { class: 'text-heading-sm uppercase text-ink-tertiary' }, 'Current audio'),
      h('h2', { class: 'font-display text-display-md text-ink-primary mt-1' }, 'Source and master'),
    ),
    h(
      'div',
      { class: 'grid grid-cols-1 lg:grid-cols-2 gap-5' },
      h(
        'div',
        { class: 'panel p-6' },
        h('div', { class: 'text-heading-sm uppercase text-ink-tertiary' }, 'Selected source'),
        h('div', { class: 'text-heading-lg text-ink-primary mt-2' }, sourceLabel),
        h('p', { class: 'text-body-sm text-ink-secondary mt-2' }, sourceDetail),
      ),
      h(
        'div',
        { class: 'panel p-6 flex flex-col gap-4' },
        h(
          'div',
          null,
          h('div', { class: 'text-heading-sm uppercase text-ink-tertiary' }, 'Full-length audio reference'),
          h('div', { class: 'text-heading-lg text-ink-primary mt-2' }, title),
          h('p', { class: 'text-body-sm text-ink-secondary mt-2' }, detail),
        ),
        delivery && playable
          ? h(
              'div',
              { class: 'grid grid-cols-2 gap-3' },
              metric('File', selectedUrl ? delivery.selected_audio?.filename || 'Selected audio' : delivery.filename || 'Historical MP3'),
              metric('Size', formatBytes(selectedUrl ? delivery.selected_audio?.size_bytes : delivery.size_bytes)),
            )
          : null,
        delivery && playable ? audioPlayer(delivery, controls) : null,
      ),
    ),
  );
}

function renderDelivery(context: SurfaceContext): HTMLElement {
  const delivery = context.delivery();
  const quality = context.quality();
  return h(
    'section',
    { class: 'flex flex-col gap-5' },
    h(
      'div',
      null,
      h('div', { class: 'text-heading-sm uppercase text-ink-tertiary' }, 'Quality and release'),
      h('h2', { class: 'font-display text-display-md text-ink-primary mt-1' }, 'Current release files'),
    ),
    QualityReview({
      episodeId: context.episodeId,
      quality,
      onUpdated: context.refreshAll,
      controls: context.controls.quality,
    }),
    clipReviewEntry(context.episodeId, quality),
    delivery ? trimDetails(delivery, context.controls, context.saveTrim) : loadingPanel('Loading episode range…'),
    delivery ? videoDetails(delivery, quality, context.controls, context.prepareVideo) : loadingPanel('Loading release video…'),
  );
}

function createMetadataEditor(initialEpisode: UnknownRecord, episodeId: string): HTMLElement {
  let saved = metadataValues(initialEpisode);
  const draft = signal<DraftState>({
    ...saved,
    saving: false,
    dirty: false,
    revision: 0,
  });
  const saveError = signal<string | null>(null);
  const saveBar = h('div');

  async function save(): Promise<void> {
    const submittedDraft = draft.peek();
    const submitted = metadataDraftValues(submittedDraft);
    const payload = buildMetadataPatch(saved, submitted);
    const fields = Object.keys(payload) as MetadataField[];
    if (!fields.length) {
      draft.set({ ...submittedDraft, dirty: false });
      return;
    }
    const submittedRevision = submittedDraft.revision;
    saveError.set(null);
    draft.set({ ...submittedDraft, saving: true });
    try {
      await api.updateEpisode(episodeId, payload);
      saved = acceptSavedPatch(saved, submitted, fields);
      const current = draft.peek();
      const changedDuringSave = current.revision !== submittedRevision;
      const stillChanged = Object.keys(buildMetadataPatch(saved, metadataDraftValues(current))).length > 0;
      draft.set({
        ...current,
        saving: false,
        dirty: changedDuringSave || stillChanged,
      });
      showToast('Metadata saved.', 'success');
    } catch (error) {
      const message = (error as Error).message;
      draft.set({ ...draft.peek(), saving: false });
      saveError.set(message);
      showToast(message, 'error');
    }
  }

  effect(() => {
    const current = draft();
    const error = saveError();
    saveBar.replaceChildren(
      h(
        'div',
        { class: 'flex-1 min-w-0' },
        error
          ? h('p', { class: 'text-body-sm text-status-danger', role: 'alert' }, `Could not save episode copy: ${error}`)
          : current.dirty
            ? h('p', { class: 'text-body-sm text-status-warning' }, 'Unsaved changes.')
            : h('p', { class: 'text-body-sm text-ink-tertiary' }, 'Changes save only when you hit Save.'),
      ),
      Button({
        variant: 'primary',
        label: current.saving ? 'Saving…' : 'Save metadata',
        loading: current.saving,
        disabled: !current.dirty || current.saving,
        onClick: () => void save(),
      }),
    );
  });

  return h(
    'div',
    { class: 'flex flex-col gap-5' },
    h(
      'div',
      { class: 'grid grid-cols-1 lg:grid-cols-2 gap-5' },
      metadataGroup('Guest', metadataText(draft, saveError, 'guest_name', 'Name'), metadataText(draft, saveError, 'guest_title', 'Title / role')),
      metadataGroup(
        'Episode',
        metadataText(draft, saveError, 'episode_name', 'Short name'),
        metadataTextarea(draft, saveError, 'episode_description', 'Short description'),
      ),
      metadataGroup(
        'Longform',
        metadataText(draft, saveError, 'title', 'Full title (YouTube / Spotify)'),
        metadataTextarea(draft, saveError, 'description', 'Full description', 6),
        metadataText(draft, saveError, 'tags', 'Tags', 'comma-separated'),
      ),
      metadataGroup(
        'Links',
        metadataText(draft, saveError, 'youtube_longform_url', 'YouTube longform URL', 'Pasted automatically after YouTube processing'),
        metadataText(draft, saveError, 'spotify_longform_url', 'Spotify longform URL'),
        metadataText(draft, saveError, 'link_tree_url', 'Link tree'),
      ),
    ),
    h(
      'div',
      {
        class: 'sticky bottom-0 panel px-5 py-4 flex items-center gap-4 z-10',
      },
      saveBar,
    ),
  );
}

function renderPublication(context: SurfaceContext, approving: Signal<boolean>, approvalError: Signal<string | null>): HTMLElement {
  const quality = context.quality();
  const review = context.review();
  const schedule = context.schedule();
  const busy = approving();
  const error = approvalError();
  const gate = quality?.release_gate;
  const canApprove = gate?.status === 'awaiting_publish_approval' && gate.can_approve_publish;
  const blockers = gate?.blockers ?? [];
  const projection = episodeScheduleProjection(schedule, context.episodeId);

  return h(
    'div',
    { class: 'grid grid-cols-1 xl:grid-cols-[1fr_1.2fr] gap-5' },
    h(
      'div',
      { class: 'panel p-5 flex flex-col gap-4' },
      h('div', { class: 'text-heading-md text-ink-primary' }, 'Approval gate'),
      h(
        'p',
        {
          class: `text-body ${canApprove ? 'text-status-success' : 'text-ink-secondary'}`,
        },
        canApprove
          ? 'The canonical release gate is ready for approval.'
          : gate
            ? `Current gate: ${gate.status.replaceAll('_', ' ')}.`
            : 'Loading the canonical release gate…',
      ),
      blockers.length
        ? h(
            'div',
            {
              class: 'rounded-md bg-status-warning/10 border border-status-warning/30 px-4 py-3',
            },
            ...blockers.map((blocker) => h('p', { class: 'text-body-sm text-ink-secondary py-1' }, blocker.message)),
          )
        : null,
      error ? h('p', { class: 'text-body-sm text-status-danger', role: 'alert' }, `Could not record approval: ${error}`) : null,
      canApprove
        ? Button({
            variant: 'primary',
            size: 'lg',
            label: busy ? 'Recording approval…' : 'Approve release',
            loading: busy,
            disabled: busy,
            onClick: async () => {
              approving.set(true);
              approvalError.set(null);
              try {
                await api.approvePublish(context.episodeId, {
                  start_publication: false,
                });
                showToast('Release approval recorded.', 'success');
                await context.refreshAll();
              } catch (caught) {
                const message = (caught as Error).message;
                approvalError.set(message);
                showToast(message, 'error');
              } finally {
                approving.set(false);
              }
            },
          })
        : null,
      h(
        'p',
        { class: 'text-body-sm text-ink-tertiary' },
        'Approval records the exact current release revision. It does not choose destinations or start publication.',
      ),
      actionLink('/schedule', 'Open Schedule and history', 'Inspect queue states, provider receipts, confirmed URLs, and failures'),
    ),
    renderScheduleEvidence(projection.items, projection.publicationEvidence, projection.blockers, review),
  );
}

function renderScheduleEvidence(
  items: EpisodeScheduleItem[],
  evidence: EpisodePublicationEvidence[],
  blockers: string[],
  review: EpisodeReviewState | null,
): HTMLElement {
  const selected = review?.clips.filter((clip) => clip.review.selection.status === 'selected') ?? [];
  const ready = selected.filter((clip) => clipDistributionReady(clip.review));
  return h(
    'div',
    { class: 'panel p-5 flex flex-col gap-4' },
    h(
      'div',
      null,
      h('div', { class: 'text-heading-md text-ink-primary' }, 'Release evidence'),
      h(
        'p',
        { class: 'text-body-sm text-ink-tertiary mt-1' },
        `${ready.length} of ${selected.length} selected short versions are current and approved. Provider states below come only from Schedule.`,
      ),
    ),
    blockers.length
      ? h(
          'div',
          {
            class: 'rounded-md border border-status-warning/30 bg-status-warning/10 px-4 py-3',
          },
          ...blockers.map((message) => h('p', { class: 'text-body-sm text-ink-secondary py-1' }, message)),
        )
      : null,
    items.length
      ? h(
          'div',
          null,
          h('div', { class: 'text-heading-sm uppercase text-ink-tertiary mb-2' }, 'Schedule items'),
          h('div', { class: 'divide-y divide-border-subtle' }, ...items.map(scheduleItemRow)),
        )
      : h('p', { class: 'text-body-sm text-ink-tertiary' }, 'No queue items for this episode are recorded in Schedule.'),
    evidence.length
      ? h(
          'div',
          null,
          h('div', { class: 'text-heading-sm uppercase text-ink-tertiary mb-2' }, 'Recorded provider activity'),
          h('div', { class: 'divide-y divide-border-subtle' }, ...evidence.map(publicationEvidenceRow)),
        )
      : h('p', { class: 'text-body-sm text-ink-tertiary' }, 'No provider publication evidence for this episode is recorded in Schedule.'),
  );
}

function renderBackup(
  episode: UnknownRecord,
  episodeId: string,
  approving: Signal<boolean>,
  phrase: Signal<string>,
  backupError: Signal<string | null>,
  confirmationInput: HTMLInputElement,
): HTMLElement {
  const status = describeStatus(episode.status as string);
  const canBackup = status.key === 'awaiting_backup';
  const busy = approving();
  const confirmed = backupConfirmationMatches(phrase());
  const error = backupError();

  return h(
    'div',
    { class: 'grid grid-cols-1 xl:grid-cols-[1.4fr_1fr] gap-5' },
    h(
      'div',
      { class: 'panel p-6 flex flex-col gap-4' },
      h('div', { class: 'text-heading-sm uppercase text-ink-tertiary' }, 'What gets copied'),
      h(
        'p',
        { class: 'text-body text-ink-secondary' },
        'Everything on SSD for this episode is rsync’d to the Seagate drive so the original footage can be freed from the SD cards. Takes 5–15 minutes.',
      ),
      h(
        'div',
        { class: 'divide-y divide-border-subtle' },
        ...BACKUP_ARTIFACTS.map((artifact) =>
          h(
            'div',
            { class: 'py-2.5' },
            h('div', { class: 'text-body text-ink-primary font-medium' }, artifact.label),
            h('div', { class: 'text-body-sm text-ink-tertiary' }, artifact.detail),
          ),
        ),
      ),
    ),
    h(
      'div',
      { class: 'flex flex-col gap-4' },
      h(
        'div',
        { class: 'panel p-5 flex flex-col gap-3' },
        h(
          'div',
          { class: 'flex items-center gap-3' },
          h('div', { class: 'text-heading-sm uppercase text-ink-tertiary' }, 'Paths'),
          StatusPill({ descriptor: status, size: 'sm' }),
        ),
        pathRow('Source', `/Volumes/1TB_SSD/cascade/episodes/${episodeId}/`),
        pathRow('Target', BACKUP_TARGET_PATH),
        pathRow('Duration', formatDuration(episode.duration_seconds as number)),
      ),
      canBackup
        ? h(
            'div',
            {
              class: 'panel p-5 flex flex-col gap-3 border-status-warning/30',
            },
            h('div', { class: 'text-body text-status-warning font-medium' }, 'Confirm to run'),
            h(
              'p',
              { class: 'text-body-sm text-ink-secondary' },
              'Type ',
              h(
                'code',
                {
                  class: 'bg-surface-inset px-1.5 py-0.5 rounded text-code-sm text-ink-primary',
                },
                'back it up',
              ),
              ' to enable the run button.',
            ),
            confirmationInput,
            error ? h('p', { class: 'text-body-sm text-status-danger', role: 'alert' }, `Could not start backup: ${error}`) : null,
            Button({
              variant: 'primary',
              size: 'lg',
              label: busy ? 'Backing up…' : 'Back it up',
              loading: busy,
              disabled: !confirmed || busy,
              onClick: async () => {
                approving.set(true);
                backupError.set(null);
                try {
                  await api.approveBackup(episodeId);
                  showToast('Backup started — pipeline is copying to Seagate.', 'success');
                  navigate(episodeSectionPath(episodeId, 'review'));
                } catch (caught) {
                  const message = (caught as Error).message;
                  backupError.set(message);
                  showToast(message, 'error');
                } finally {
                  approving.set(false);
                }
              },
            }),
          )
        : h(
            'div',
            { class: 'panel p-5' },
            h('div', { class: 'text-body text-ink-primary font-medium mb-1' }, 'Not ready yet'),
            h('p', { class: 'text-body-sm text-ink-secondary' }, `Current status: ${status.label}. ${status.hint}`),
          ),
    ),
  );
}

function trimDetails(delivery: DeliveryStatus, controls: SurfaceControls, saveTrim: (start: number, end: number) => Promise<void>): HTMLElement {
  const busy = delivery.video_status === 'preparing';
  controls.trimStart = stableControl(
    controls.trimStart,
    'trim-start',
    () =>
      h('input', {
        value: formatEpisodeTimestamp(delivery.trim_start_seconds ?? 0),
        class: 'w-full h-10 bg-surface-2 border border-border rounded-md px-3 text-body text-ink-primary focus:border-accent focus:outline-none',
        'aria-label': 'Episode start time',
      }) as HTMLInputElement,
  );
  controls.trimEnd = stableControl(
    controls.trimEnd,
    'trim-end',
    () =>
      h('input', {
        value: formatEpisodeTimestamp(delivery.trim_end_seconds ?? delivery.source_duration_seconds ?? 0),
        class: 'w-full h-10 bg-surface-2 border border-border rounded-md px-3 text-body text-ink-primary focus:border-accent focus:outline-none',
        'aria-label': 'Episode end time',
      }) as HTMLInputElement,
  );
  const startInput = controls.trimStart.value;
  const endInput = controls.trimEnd.value;
  return h(
    'div',
    { class: 'panel p-6 flex flex-col gap-4' },
    h(
      'div',
      null,
      h('div', { class: 'text-heading-md text-ink-primary' }, 'Episode range'),
      h(
        'p',
        { class: 'text-body-sm text-ink-tertiary mt-1' },
        `Choose the conversation start and end within the ${formatDuration(delivery.source_duration_seconds)} source. Use seconds or HH:MM:SS.`,
      ),
    ),
    h(
      'div',
      { class: 'grid grid-cols-1 sm:grid-cols-2 gap-4' },
      h('label', { class: 'text-body-sm text-ink-secondary flex flex-col gap-1.5' }, 'Start', startInput),
      h('label', { class: 'text-body-sm text-ink-secondary flex flex-col gap-1.5' }, 'End', endInput),
    ),
    Button({
      variant: 'secondary',
      label: 'Save episode range',
      disabled: busy,
      onClick: () => {
        const start = parseEpisodeTimestamp(startInput.value);
        const end = parseEpisodeTimestamp(endInput.value);
        const duration = delivery.source_duration_seconds ?? 0;
        if (start == null || end == null || !(0 <= start && start < end && end <= duration)) {
          showToast(`Enter a range between 00:00:00 and ${formatEpisodeTimestamp(duration)}.`, 'error');
          return;
        }
        void saveTrim(start, end);
      },
    }),
  );
}

function videoDetails(delivery: DeliveryStatus, quality: QualitySnapshot | null, controls: SurfaceControls, prepareVideo: () => Promise<void>): HTMLElement {
  const state = delivery.video_status ?? 'not_prepared';
  const busy = state === 'preparing';
  const progress = Math.min(99, Math.max(0, delivery.video_progress ?? 0));
  const video = delivery.video;
  const releaseArtifact = quality?.artifacts.release_video;
  return h(
    'div',
    { class: 'panel p-6 flex flex-col gap-5' },
    h(
      'div',
      { class: 'flex items-start justify-between gap-4 flex-wrap' },
      h(
        'div',
        null,
        h(
          'div',
          { class: 'text-heading-md text-ink-primary' },
          state === 'ready' ? 'Rendered video available' : busy ? 'Preparing release video' : state === 'failed' ? 'Video preparation failed' : 'Release video',
        ),
        h(
          'p',
          { class: 'text-body-sm text-ink-tertiary mt-1 max-w-[680px]' },
          busy
            ? `${delivery.video_detail || 'Encoding'} · ${progress.toFixed(0)}%`
            : state === 'ready' && video?.render_mode === 'speaker_cut'
              ? 'Speaker-cut 1080p render with saved edits and mastered audio.'
              : state === 'ready'
                ? 'An earlier render is available. Prepare again to build the current speaker-cut video.'
                : 'Prepare a speaker-cut 1080p video with saved edits and mastered audio.',
        ),
      ),
      Button({
        variant: 'primary',
        size: 'lg',
        label: busy ? 'Preparing…' : state === 'ready' ? 'Prepare again' : 'Prepare video',
        loading: busy,
        disabled: busy,
        onClick: () => void prepareVideo(),
      }),
    ),
    busy
      ? h(
          'div',
          { class: 'h-2 rounded-full bg-surface-3 overflow-hidden' },
          h('div', {
            class: 'h-full bg-accent transition-all',
            style: { width: `${Math.max(1, progress)}%` },
          }),
        )
      : null,
    delivery.video_error
      ? h(
          'div',
          {
            class: 'rounded-md bg-status-danger/10 border border-status-danger/30 p-4 text-body text-status-danger',
          },
          delivery.video_error,
        )
      : null,
    releaseArtifact
      ? h(
          'div',
          {
            class: [
              'rounded-md border px-4 py-3 text-body-sm',
              releaseArtifact.ready
                ? 'border-status-success/30 bg-status-success/10 text-ink-secondary'
                : 'border-status-warning/30 bg-status-warning/10 text-status-warning',
            ].join(' '),
          },
          releaseArtifact.detail,
        )
      : null,
    state === 'ready' && video
      ? h(
          'div',
          { class: 'border-t border-border pt-5 flex flex-col gap-4' },
          videoPlayer(delivery, controls),
          h(
            'div',
            { class: 'grid grid-cols-2 sm:grid-cols-3 gap-4' },
            metric('Duration', formatDuration(video.duration_seconds)),
            metric('File size', formatBytes(video.size_bytes)),
            metric('Resolution', `${video.width}×${video.height}`),
            metric('Codecs', `${video.video_codec.toUpperCase()} / ${video.audio_codec.toUpperCase()}`),
            metric('Saved edits applied', String(video.edit_count)),
          ),
          h(
            'a',
            {
              href: delivery.video_download_url,
              download: video.filename,
              class: 'inline-flex h-11 px-5 items-center justify-center self-start rounded-md bg-accent text-ink-on-accent font-medium hover:brightness-110',
            },
            'Download rendered video',
          ),
        )
      : null,
  );
}

function clipReviewEntry(episodeId: string, quality: QualitySnapshot | null): HTMLElement | null {
  const rendered = quality?.artifacts.rendered_short_count ?? 0;
  const candidates = quality?.artifacts.candidate_count ?? 0;
  if (!rendered && !candidates) return null;
  const firstClip = quality?.artifacts.rendered_short_ids[0];
  const count = rendered || candidates;
  const path = firstClip
    ? `/episodes/${encodeURIComponent(episodeId)}/clips/review/${encodeURIComponent(firstClip)}`
    : `/episodes/${encodeURIComponent(episodeId)}/clips/review`;
  return h(
    'div',
    {
      class: 'panel p-5 flex items-center justify-between gap-5 flex-wrap border-accent/40',
    },
    h(
      'div',
      null,
      h(
        'div',
        { class: 'text-heading-md text-ink-primary' },
        rendered ? `${pluralize(count, 'rendered clip')} ready to watch` : `${pluralize(count, 'clip candidate')} ready to review`,
      ),
      h('p', { class: 'text-body-sm text-ink-tertiary mt-1' }, 'Open the specialist review for playback, selection, captions, variants, and approvals.'),
    ),
    h(
      'a',
      {
        ...link(path),
        class: 'inline-flex h-11 px-5 items-center justify-center rounded-md bg-accent text-ink-on-accent text-body-lg font-medium hover:brightness-110',
      },
      rendered ? 'Watch clips' : 'Review clips',
    ),
  );
}

function audioPlayer(delivery: DeliveryStatus, controls: SurfaceControls): HTMLAudioElement {
  const source = delivery.selected_audio_download_url || delivery.download_url;
  const identity = `${source ?? ''}:${delivery.completed_at ?? ''}`;
  controls.audio = stableControl(
    controls.audio,
    identity,
    () =>
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
      }) as HTMLAudioElement,
  );
  return controls.audio.value;
}

function videoPlayer(delivery: DeliveryStatus, controls: SurfaceControls): HTMLVideoElement {
  const identity = `${delivery.video_download_url ?? ''}:${delivery.video_completed_at ?? ''}`;
  controls.video = stableControl(
    controls.video,
    identity,
    () =>
      h('video', {
        controls: true,
        preload: 'metadata',
        src: delivery.video_download_url,
        class: 'w-full rounded-md bg-black',
      }) as HTMLVideoElement,
  );
  return controls.video.value;
}

function detailsSection(key: EpisodeSection, title: string, description: string, body: HTMLElement): HTMLDetailsElement {
  return h(
    'details',
    {
      class: 'panel scroll-mt-44 focus:outline-none',
      dataset: { section: key },
      tabindex: '-1',
    },
    h(
      'summary',
      { class: 'cursor-pointer px-6 py-5' },
      h('div', { class: 'text-heading-md text-ink-primary' }, title),
      h('p', { class: 'text-body-sm text-ink-tertiary mt-1' }, description),
    ),
    h('div', { class: 'border-t border-border-subtle p-6' }, body),
  ) as HTMLDetailsElement;
}

function section(key: EpisodeSection): HTMLElement {
  return h('section', {
    class: 'scroll-mt-44 focus:outline-none',
    dataset: { section: key },
    tabindex: '-1',
  });
}

function actionLink(path: string, title: string, detail: string): HTMLElement {
  return h(
    'a',
    {
      ...link(path),
      class: 'rounded-md border border-border-subtle bg-surface-2 px-4 py-3 hover:border-border-strong flex items-center gap-4',
    },
    h(
      'div',
      { class: 'flex-1 min-w-0' },
      h('div', { class: 'text-body text-ink-primary font-medium' }, title),
      h('div', { class: 'text-body-sm text-ink-tertiary mt-0.5' }, detail),
    ),
    Icon.chevronRight({ size: 16 }),
  );
}

function fact(label: string, value: string, detail: string, tone: 'success' | 'warning' | 'neutral'): HTMLElement {
  const toneClass = tone === 'success' ? 'text-status-success' : tone === 'warning' ? 'text-status-warning' : 'text-ink-primary';
  return h(
    'div',
    {
      class: 'rounded-md border border-border-subtle bg-surface-2 px-4 py-4',
    },
    h('div', { class: 'text-heading-sm uppercase text-ink-tertiary' }, label),
    h('div', { class: `text-heading-md mt-2 ${toneClass}` }, value),
    h('p', { class: 'text-body-sm text-ink-secondary mt-2' }, detail),
  );
}

function metric(label: string, value: string): HTMLElement {
  return h(
    'div',
    null,
    h('div', { class: 'text-body-sm text-ink-tertiary' }, label),
    h('div', { class: 'text-body text-ink-primary font-medium break-words' }, value),
  );
}

function metadataGroup(title: string, ...fields: HTMLElement[]): HTMLElement {
  return h('section', { class: 'panel p-5 flex flex-col gap-4 min-w-0' }, h('div', { class: 'text-heading-sm uppercase text-ink-tertiary' }, title), ...fields);
}

function metadataText(draft: Signal<DraftState>, saveError: Signal<string | null>, name: MetadataField, label: string, hint?: string): HTMLElement {
  const input = h('input', {
    id: `metadata-${name}`,
    type: 'text',
    value: draft.peek()[name],
    class: 'w-full h-10 bg-surface-2 border border-border rounded-md px-3 text-body text-ink-primary focus:border-accent focus:outline-none',
    oninput: (event: Event) => {
      const current = draft.peek();
      saveError.set(null);
      draft.set({
        ...current,
        [name]: (event.target as HTMLInputElement).value,
        dirty: true,
        revision: current.revision + 1,
      } as DraftState);
    },
  });
  return h(
    'div',
    { class: 'flex flex-col gap-1.5 min-w-0' },
    h(
      'label',
      {
        for: `metadata-${name}`,
        class: 'text-body-sm text-ink-secondary font-medium',
      },
      label,
    ),
    input,
    hint ? h('p', { class: 'text-body-sm text-ink-tertiary' }, hint) : null,
  );
}

function metadataTextarea(draft: Signal<DraftState>, saveError: Signal<string | null>, name: MetadataField, label: string, rows = 3): HTMLElement {
  const input = h('textarea', {
    id: `metadata-${name}`,
    rows: String(rows),
    value: draft.peek()[name],
    class:
      'w-full bg-surface-2 border border-border rounded-md px-3 py-2 text-body text-ink-primary leading-relaxed focus:border-accent focus:outline-none resize-vertical',
    oninput: (event: Event) => {
      const current = draft.peek();
      saveError.set(null);
      draft.set({
        ...current,
        [name]: (event.target as HTMLTextAreaElement).value,
        dirty: true,
        revision: current.revision + 1,
      } as DraftState);
    },
  });
  return h(
    'div',
    { class: 'flex flex-col gap-1.5 min-w-0' },
    h(
      'label',
      {
        for: `metadata-${name}`,
        class: 'text-body-sm text-ink-secondary font-medium',
      },
      label,
    ),
    input,
  );
}

function metadataDraftValues(draft: DraftState): MetadataValues {
  const { saving: _saving, dirty: _dirty, revision: _revision, ...values } = draft;
  return values;
}

function scheduleItemRow(item: EpisodeScheduleItem): HTMLElement {
  const destinations = item.destinations ?? (item.destination ? [item.destination] : []);
  return h(
    'div',
    { class: 'py-2.5 text-body-sm' },
    h(
      'div',
      { class: 'flex items-center justify-between gap-3' },
      h(
        'span',
        { class: 'text-ink-primary font-medium' },
        item.type === 'short' && item.clip_id ? `Short ${item.clip_id}` : item.type === 'longform' ? 'Longform' : item.type,
      ),
      h('span', { class: 'text-ink-secondary font-mono' }, String(item.state ?? 'suggested').replaceAll('_', ' ')),
    ),
    destinations.length ? h('div', { class: 'text-ink-tertiary mt-1' }, destinations.map((value) => value.replaceAll('_', ' ')).join(', ')) : null,
    item.scheduled_date ? h('div', { class: 'text-ink-tertiary mt-1 font-mono' }, item.scheduled_date) : null,
    item.artifact_current === false ? h('div', { class: 'text-status-warning mt-1' }, 'Scheduled media differs from the current selected version') : null,
    item.error ? h('div', { class: 'text-status-danger mt-1' }, item.error) : null,
  );
}

function publicationEvidenceRow(record: EpisodePublicationEvidence): HTMLElement {
  const content =
    record.content_type === 'podcast_audio'
      ? 'Podcast RSS audio'
      : record.content_type === 'longform'
        ? 'Longform'
        : record.clip_id
          ? `Short ${record.clip_id}`
          : 'Short';
  const destinations = record.destinations ?? (record.destination ? [record.destination] : ['unknown destination']);
  const safeUrl = record.url?.startsWith('https://') || record.url?.startsWith('http://') ? record.url : null;
  const line = h(
    'span',
    { class: 'text-body-sm text-ink-secondary' },
    `${content} · ${destinations.map((value) => value.replaceAll('_', ' ')).join(', ')} · ${publicationEvidenceStatusLabel(record.status, record.scheduled)}${record.job_id ? ` · Job ${record.job_id}` : record.request_id ? ` · Request ${record.request_id}` : ''}${record.error ? ` · ${record.error}` : ''}`,
  );
  return h(
    'div',
    { class: 'py-2.5' },
    safeUrl
      ? h(
          'a',
          {
            href: safeUrl,
            target: '_blank',
            rel: 'noreferrer',
            class: 'hover:text-accent',
          },
          line,
        )
      : line,
  );
}

function pathRow(label: string, value: string): HTMLElement {
  return h(
    'div',
    { class: 'flex flex-col gap-0.5' },
    h('div', { class: 'text-body-sm text-ink-tertiary' }, label),
    h(
      'div',
      {
        class: 'text-code text-ink-primary font-mono tabular break-all leading-snug',
      },
      value,
    ),
  );
}

function audioInventory(episode: UnknownRecord): {
  cameraChannels: number;
  fileCount: number;
  recorderTracks: number;
} {
  const tracks = (episode.audio_tracks as Array<Record<string, unknown>>) ?? [];
  const recorderTracks = new Set(
    tracks
      .filter((track) => track.track_type === 'input')
      .map((track) => finiteNumber(track.track_number))
      .filter((track): track is number => track != null),
  );
  return {
    cameraChannels: tracks.filter((track) => track.track_type === 'camera_channel').length,
    fileCount: tracks.length,
    recorderTracks: recorderTracks.size,
  };
}

function baseSourceDescription(inventory: { cameraChannels: number; fileCount: number; recorderTracks: number }): [string, string] {
  if (inventory.recorderTracks > 0) {
    return [
      'Base recorder mix',
      `${pluralize(inventory.recorderTracks, 'recorder track')} routed with ${pluralize(inventory.cameraChannels, 'camera channel')}. No repair draft is selected.`,
    ];
  }
  if (inventory.cameraChannels > 0) {
    return [
      'Camera-source audio',
      `${pluralize(inventory.cameraChannels, 'camera channel')} on the source clock. No external recorder mix or repair draft is selected.`,
    ];
  }
  return [
    'Base audio source',
    inventory.fileCount
      ? `${pluralize(inventory.fileCount, 'audio file')} extracted. No repair draft is selected.`
      : 'No extracted audio inventory or selected repair is available.',
  ];
}

function repairDescription(selection: NonNullable<QualitySnapshot['audio_quality']['repair_selection']>): [string, string] {
  const status = selection.status === 'stale' ? 'Selected repair draft · stale' : 'Selected repair draft';
  const detail = selection.detail
    ? String(selection.detail)
    : selection.release_safe
      ? 'Revision-bound repair selected for future renders.'
      : 'Repair selection exists but is not release-safe for the current revision.';
  return [status, detail];
}

function speakerCount(episode: UnknownRecord): string {
  const config = episode.crop_config as Record<string, unknown> | undefined;
  const speakers = config?.speakers as unknown[] | undefined;
  if (speakers) return pluralize(speakers.length, 'speaker');
  if (config?.speaker_l_center_x != null) return '2 speakers';
  return 'Not set';
}

function shouldOfferPreparationRetry(
  episode: UnknownRecord,
  pipeline: Record<string, unknown> | undefined,
  completed: string[],
  errors: Record<string, string>,
): boolean {
  const status = String(episode.status ?? '');
  if (status === 'error' || status === 'cancelled') return true;
  if (['ingest', 'stitch', 'audio_analysis'].some((name) => errors[name])) {
    return true;
  }
  if (status !== 'processing' || pipeline?.current_agent) return false;
  if (!pipeline?.agents_requested) return true;
  const started = Date.parse(String(pipeline.started_at ?? episode.created_at ?? ''));
  return Number.isFinite(started) && Date.now() - started > 120_000 && !['ingest', 'stitch', 'audio_analysis'].every((name) => completed.includes(name));
}

function finiteNumber(value: unknown): number | null {
  const number = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(number) ? number : null;
}

function formatBytes(value?: number): string {
  return value == null ? '—' : `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function projectionLabel(key: ProjectionKey): string {
  if (key === 'review') return 'Could not refresh editorial review';
  if (key === 'quality') return 'Could not refresh quality state';
  if (key === 'delivery') return 'Could not refresh release files';
  return 'Could not refresh Schedule evidence';
}

function loadingPanel(label: string): HTMLElement {
  return h('div', { class: 'panel p-6 animate-pulse-breath text-ink-tertiary' }, label);
}

function inlineError(message: string): HTMLElement {
  return h(
    'div',
    {
      class: 'rounded-md border border-status-danger/30 bg-status-danger/10 px-4 py-3 text-body text-status-danger mb-3',
      role: 'alert',
    },
    message,
  );
}

function loadingHeader(): HTMLElement {
  return h(
    'div',
    { class: 'max-w-[1280px] mx-auto' },
    h('div', {
      class: 'h-6 w-40 bg-surface-2 rounded-md animate-pulse-breath',
    }),
    h('div', {
      class: 'h-9 w-80 max-w-full bg-surface-2 rounded-md mt-3 animate-pulse-breath',
    }),
  );
}

function errorHeader(error: string): HTMLElement {
  return h('div', { class: 'max-w-[1280px] mx-auto' }, inlineError(error));
}

function loadingBody(): HTMLElement {
  return h(
    'div',
    { class: 'grid grid-cols-1 lg:grid-cols-2 gap-5' },
    h('div', { class: 'panel h-52 animate-pulse-breath' }),
    h('div', { class: 'panel h-52 animate-pulse-breath' }),
  );
}
