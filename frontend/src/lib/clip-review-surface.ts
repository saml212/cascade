import type {
  ActiveClipVariantId,
  ClipReReleaseRequestState,
  ClipReviewState,
  ClipVariantId,
  PrepareClipReReleaseRequest,
  PrepareClipReReleaseResponse,
  ReviewArtifact,
  ShortVariantReview,
} from './api';
import type { StatusDescriptor } from './format';

export type ClipReviewSurface = 'base' | ClipVariantId;

const CLIP_VARIANT_IDS: readonly ClipVariantId[] = [
  'gameplay_surround_v1',
  'speaker_panels_v1',
  'background_motion_v1',
  'satisfying_motion_v1',
  'minecraft_parkour_v1',
  'subway_surfers_v1',
  'gta_driving_v1',
];

const CLIP_VARIANT_LABELS: Readonly<Record<ClipVariantId, string>> = {
  background_motion_v1: 'Motion background',
  satisfying_motion_v1: 'Satisfying footage',
  minecraft_parkour_v1: 'Minecraft parkour',
  subway_surfers_v1: 'Subway Surfers',
  gta_driving_v1: 'GTA driving',
  gameplay_surround_v1: 'Gameplay surround',
  speaker_panels_v1: 'Clean speaker panels',
};

export const RETIRED_VARIANT_NOTICE =
  'Retired \u2014 existing media and history only';

export function isActiveClipVariantId(
  value: unknown
): value is ActiveClipVariantId {
  return value === 'gameplay_surround_v1' || value === 'speaker_panels_v1';
}

function isClipVariantId(value: unknown): value is ClipVariantId {
  return CLIP_VARIANT_IDS.some((variantId) => value === variantId);
}

export function clipVariantForSurface(
  review: ClipReviewState,
  surface: ClipReviewSurface
): ShortVariantReview | undefined {
  if (surface === 'base' || !isClipVariantId(surface)) return undefined;
  const variant = review.variants?.[surface];
  return variant?.id === surface ? variant : undefined;
}

export function clipVariantSurfaces(
  review: ClipReviewState
): ClipVariantId[] {
  return CLIP_VARIANT_IDS.filter((variantId) =>
    Boolean(clipVariantForSurface(review, variantId))
  );
}

export interface ClipVersionState {
  surface: ClipReviewSurface;
  version: 'base' | ClipVariantId;
  variantId: null | ClipVariantId;
  label: string;
  activeForNewWrites: boolean;
  render: ReviewArtifact;
  approval: { status: string; current: boolean; revision: string };
}

export function clipVersionState(
  review: ClipReviewState,
  surface: ClipReviewSurface
): ClipVersionState | null {
  if (surface === 'base') {
    return {
      surface,
      version: 'base',
      variantId: null,
      label: 'Base',
      activeForNewWrites: true,
      render: review.render,
      approval: review.approval,
    };
  }
  const variant = clipVariantForSurface(review, surface);
  if (!variant) return null;
  return {
    surface,
    version: variant.id,
    variantId: variant.id,
    label: variant.label || CLIP_VARIANT_LABELS[variant.id],
    activeForNewWrites:
      isActiveClipVariantId(variant.id) &&
      variant.active_for_new_writes === true,
    render: variant.render,
    approval: variant.approval,
  };
}

export function clipVersionAllowsNewWrites(
  review: ClipReviewState,
  surface: ClipReviewSurface
): boolean {
  return clipVersionState(review, surface)?.activeForNewWrites === true;
}

export function selectedDistributionVersion(
  review: ClipReviewState
): ClipVersionState | null {
  const distribution = review.distribution;
  if (!distribution) return null;
  if (distribution.version === 'base' && distribution.variant_id === null) {
    return clipVersionState(review, 'base');
  }
  if (
    isClipVariantId(distribution.version) &&
    distribution.variant_id === distribution.version
  ) {
    return clipVersionState(review, distribution.version);
  }
  return null;
}

/** Require the exact selected render and its own revision-bound approval. */
export function clipDistributionReady(review: ClipReviewState): boolean {
  const version = selectedDistributionVersion(review);
  const distribution = review.distribution;
  return Boolean(
    version &&
      clipVersionAllowsNewWrites(review, version.surface) &&
      distribution.active_for_new_writes === true &&
      distribution.current &&
      distribution.approval_current &&
      version.render.current &&
      version.approval.current &&
      version.approval.revision === distribution.revision
  );
}

export function clipDistributionLabel(review: ClipReviewState): string {
  return selectedDistributionVersion(review)?.label ?? 'Unknown version';
}

export function distributionChangeLockReason(
  review: ClipReviewState
): string | null {
  if (review.distribution.change_locked === false) return null;
  if (review.distribution.change_locked === true) {
    const reReleaseReason = review.distribution.re_release_reason;
    if (
      review.distribution.re_release_allowed === false &&
      typeof reReleaseReason === 'string' &&
      reReleaseReason.trim()
    ) {
      const trimmed = reReleaseReason.trim();
      return trimmed === 'Prior receipts have unresolved remote destinations.'
        ? 'Existing posts are queued or need confirmation. Resolve them before changing this version.'
        : trimmed;
    }
  }
  const reason = review.distribution.change_lock_reason;
  if (typeof reason === 'string' && reason.trim()) return reason.trim();
  return review.distribution.change_locked === true
    ? 'Publication history locks this version. Start an explicit re-release to change it.'
    : 'Version change status is unavailable. Refresh before changing distribution.';
}

export function clipDistributionSelectable(
  review: ClipReviewState,
  surface: ClipReviewSurface
): boolean {
  const version = clipVersionState(review, surface);
  return Boolean(
    version &&
      clipVersionAllowsNewWrites(review, surface) &&
      selectedDistributionVersion(review)?.surface !== surface &&
      review.distribution.change_locked === false &&
      review.distribution.re_release_request === null &&
      version.render.current &&
      version.approval.current
  );
}

export function distributionVersionLabel(
  version: unknown,
  variantId: unknown
): string {
  if (isClipVariantId(version) && variantId === version) {
    return CLIP_VARIANT_LABELS[version];
  }
  if ((version == null || version === 'base') && variantId == null) return 'Base';
  return 'Unknown version';
}

export function clipCardStatusOverride(
  rawStatus: unknown,
  changeLocked: unknown
): StatusDescriptor | null {
  if (
    typeof rawStatus !== 'string' ||
    rawStatus.toLowerCase() !== 'approved'
  ) {
    return null;
  }
  return {
    key: 'queued',
    tone: 'neutral',
    label: 'Approved',
    hint:
      changeLocked === true
        ? 'This clip is approved. Publication-history checks lock distribution changes; review its distribution details before a re-release.'
        : 'This clip\u2019s current render is approved.',
  };
}

export function publicationEvidenceStatusLabel(
  status: unknown,
  scheduled: unknown
): string {
  if (status === 'cancellation_pending') {
    return 'Cancellation pending verification';
  }
  if (status === 'published') return 'Published URL recorded';
  if (status === 'partial_failure') return 'Some destinations failed';
  if (status === 'failed') return 'Failed';
  if (status === 'unknown') return 'Unknown outcome';
  if (scheduled === true) return 'Scheduled';
  if (status === 'submitted') return 'Submission recorded';
  if (status === 'already_submitted') return 'Prior submission recorded';
  return 'Publication record';
}

export type ClipReReleaseViewState =
  | { kind: 'unneeded' }
  | {
      kind: 'available';
      previousRequest: ClipReReleaseRequestState | null;
    }
  | { kind: 'prepared'; request: ClipReReleaseRequestState }
  | { kind: 'blocked'; reason: string };

function nonemptyString(value: unknown): value is string {
  return typeof value === 'string' && Boolean(value.trim());
}

function validReReleaseRequest(
  value: unknown
): ClipReReleaseRequestState | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const request = value as Record<string, unknown>;
  const requiredStrings = [
    'request_id',
    'actor',
    'reason',
    'target_revision',
    'render_fingerprint',
    'receipt_history_revision',
    'revision',
    'created_at',
  ];
  if (
    (request.variant_id !== null && !isClipVariantId(request.variant_id)) ||
    requiredStrings.some((field) => !nonemptyString(request[field]))
  ) {
    return null;
  }
  return request as unknown as ClipReReleaseRequestState;
}

export function confirmsClipReRelease(
  response: PrepareClipReReleaseResponse,
  clipId: string,
  request: PrepareClipReReleaseRequest
): boolean {
  const prepared = validReReleaseRequest(
    response.distribution?.re_release_request
  );
  return Boolean(
    (response.status === 'prepared' || response.status === 'already_prepared') &&
      response.requires_publish_approval === true &&
      response.clip_id === clipId &&
      (response.distribution?.re_release_request_consumed === false ||
        (response.status === 'already_prepared' &&
          response.distribution?.re_release_request_consumed === true)) &&
      response.distribution?.variant_id === request.variant_id &&
      response.distribution?.revision === request.expected_revision &&
      prepared?.request_id === request.request_id &&
      prepared.actor === request.actor &&
      prepared.reason === request.reason &&
      prepared.variant_id === request.variant_id &&
      prepared.target_revision === request.expected_revision
  );
}

export function clipReReleaseViewState(
  review: ClipReviewState
): ClipReReleaseViewState {
  const distribution = review.distribution;
  const rawRequest = distribution.re_release_request;
  const consumed = distribution.re_release_request_consumed;
  const backendReason =
    typeof distribution.re_release_reason === 'string' &&
    distribution.re_release_reason.trim()
      ? distribution.re_release_reason.trim()
      : null;
  if (rawRequest === null) {
    if (consumed !== null) {
      return {
        kind: 'blocked',
        reason:
          backendReason ??
          'Re-release request state is unavailable. Refresh before preparing another re-release.',
      };
    }
    if (distribution.change_locked !== true) return { kind: 'unneeded' };
    if (distribution.re_release_allowed === true) {
      return { kind: 'available', previousRequest: null };
    }
  } else {
    const request = validReReleaseRequest(rawRequest);
    if (!request || (consumed !== false && consumed !== true)) {
      return {
        kind: 'blocked',
        reason:
          backendReason ??
          'Re-release request state is unavailable. Refresh before preparing another re-release.',
      };
    }
    if (consumed === false) {
      if (
        request.variant_id !== distribution.variant_id ||
        request.target_revision !== distribution.revision
      ) {
        return {
          kind: 'blocked',
          reason:
            'The prepared re-release no longer matches the selected render and copy. Refresh before continuing.',
        };
      }
      return { kind: 'prepared', request };
    }
    if (distribution.re_release_allowed === true) {
      return { kind: 'available', previousRequest: request };
    }
  }
  return {
    kind: 'blocked',
    reason:
      backendReason ??
      'Re-release eligibility is unavailable. Refresh before preparing another re-release.',
  };
}

export interface ClipReReleaseDraftTarget {
  episodeId: string;
  clipId: string;
  variantId: null | ActiveClipVariantId;
  expectedRevision: string;
  previousRequestId: string | null;
}

interface KeyValueStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export class ClipReReleaseDraftStore {
  private readonly key: string;

  constructor(
    private readonly storage: KeyValueStorage,
    private readonly target: ClipReReleaseDraftTarget,
    private readonly createRequestId: () => string = () =>
      crypto.randomUUID()
  ) {
    this.key = `cascade.clip-rerelease.v1:${JSON.stringify([
      target.episodeId,
      target.clipId,
      target.variantId,
      target.expectedRevision,
      target.previousRequestId,
    ])}`;
  }

  getOrCreate(): PrepareClipReReleaseRequest {
    const stored = this.read();
    if (stored) return stored;
    const created: PrepareClipReReleaseRequest = {
      variant_id: this.target.variantId,
      expected_revision: this.target.expectedRevision,
      request_id: this.createRequestId(),
      actor: '',
      reason: '',
    };
    this.save(created);
    return created;
  }

  save(draft: PrepareClipReReleaseRequest): void {
    if (
      draft.variant_id !== this.target.variantId ||
      draft.expected_revision !== this.target.expectedRevision ||
      !nonemptyString(draft.request_id) ||
      typeof draft.actor !== 'string' ||
      typeof draft.reason !== 'string'
    ) {
      throw new Error('Re-release draft does not match the previewed version.');
    }
    this.storage.setItem(this.key, JSON.stringify(draft));
  }

  clear(): void {
    this.storage.removeItem(this.key);
  }

  private read(): PrepareClipReReleaseRequest | null {
    const encoded = this.storage.getItem(this.key);
    if (!encoded) return null;
    try {
      const value = JSON.parse(encoded) as Record<string, unknown>;
      if (
        value.variant_id !== this.target.variantId ||
        value.expected_revision !== this.target.expectedRevision ||
        !nonemptyString(value.request_id) ||
        typeof value.actor !== 'string' ||
        typeof value.reason !== 'string'
      ) {
        return null;
      }
      return value as unknown as PrepareClipReReleaseRequest;
    } catch {
      return null;
    }
  }
}

export class ClipReviewSurfaceMemory {
  private preferred: ClipReviewSurface | null = null;

  get(
    _clipId: string,
    available: ReadonlySet<ClipReviewSurface>,
    selected: ClipReviewSurface
  ): ClipReviewSurface {
    const surface = this.preferred ?? selected;
    return available.has(surface) ? surface : 'base';
  }

  select(_clipId: string, surface: ClipReviewSurface): void {
    this.preferred = surface;
  }
}
