import type { ClipReviewState, ReviewArtifact } from './api';

export type ClipReviewSurface = 'base' | 'background';

export interface ClipVersionState {
  surface: ClipReviewSurface;
  version: 'base' | 'background_motion_v1';
  variantId: null | 'background_motion_v1';
  label: string;
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
      render: review.render,
      approval: review.approval,
    };
  }
  const variant = review.variants?.background_motion_v1;
  if (!variant || variant.id !== 'background_motion_v1') return null;
  return {
    surface,
    version: 'background_motion_v1',
    variantId: 'background_motion_v1',
    label: variant.label || 'Motion background',
    render: variant.render,
    approval: variant.approval,
  };
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
    distribution.version === 'background_motion_v1' &&
    distribution.variant_id === 'background_motion_v1'
  ) {
    return clipVersionState(review, 'background');
  }
  return null;
}

/** Require the exact selected render and its own revision-bound approval. */
export function clipDistributionReady(review: ClipReviewState): boolean {
  const version = selectedDistributionVersion(review);
  const distribution = review.distribution;
  return Boolean(
    version &&
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
      selectedDistributionVersion(review)?.surface !== surface &&
      review.distribution.change_locked === false &&
      version.render.current &&
      version.approval.current
  );
}

export function distributionVersionLabel(
  version: unknown,
  variantId: unknown
): string {
  if (
    version === 'background_motion_v1' &&
    variantId === 'background_motion_v1'
  ) {
    return 'Motion background';
  }
  if ((version == null || version === 'base') && variantId == null) return 'Base';
  return 'Unknown version';
}

export class ClipReviewSurfaceMemory {
  private readonly selections = new Map<string, ClipReviewSurface>();

  get(clipId: string, hasBackground: boolean): ClipReviewSurface {
    const remembered = this.selections.get(clipId);
    return remembered === 'background' && hasBackground ? remembered : 'base';
  }

  select(clipId: string, surface: ClipReviewSurface): void {
    this.selections.set(clipId, surface);
  }
}
