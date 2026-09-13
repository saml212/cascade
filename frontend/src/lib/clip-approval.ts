type ReviewedClip = Record<string, unknown>;

export interface ClipApprovalFeedback {
  status: 'saving' | 'approved' | 'error';
  identity: string;
  message?: string;
}

type FeedbackUpdate = (feedback: ClipApprovalFeedback) => void;

function record(value: unknown): ReviewedClip {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as ReviewedClip
    : {};
}

function text(value: unknown): string | null {
  return typeof value === 'string' && value ? value : null;
}

function stableValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stableValue);
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value as ReviewedClip)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, item]) => [key, stableValue(item)])
    );
  }
  return value;
}

/** Identify the server-computed approval revision and exact rendered file. */
export function clipApprovalIdentity(clip: ReviewedClip): string {
  const review = record(clip.review);
  const approval = record(review.approval);
  const render = record(review.render);
  const revision = text(approval.revision);
  const renderFingerprint =
    text(render.recorded_fingerprint) ??
    text(render.fingerprint) ??
    text(clip.approved_render_fingerprint);
  if (revision) {
    return JSON.stringify({
      clip_id: text(clip.id) ?? text(clip.clip_id),
      revision,
      render_fingerprint: renderFingerprint,
    });
  }

  // Compatibility for a review response from a server predating `revision`.
  // Keep this bounded to editorial inputs instead of incidental response fields.
  return JSON.stringify({
    clip_id: text(clip.id) ?? text(clip.clip_id),
    approved_revision: text(clip.approved_revision),
    render_fingerprint: renderFingerprint,
    bounds: [clip.start_seconds ?? clip.start, clip.end_seconds ?? clip.end],
    copy: stableValue({
      title: clip.title,
      description: clip.description,
      hook_text: clip.hook_text,
      compelling_reason: clip.compelling_reason,
      speaker: clip.speaker,
      metadata: clip.metadata,
    }),
  });
}

export function matchingClipApprovalFeedback(
  clip: ReviewedClip,
  feedback: ClipApprovalFeedback | undefined
): ClipApprovalFeedback | undefined {
  return feedback?.identity === clipApprovalIdentity(clip) ? feedback : undefined;
}

export function clipIsApproved(
  clip: ReviewedClip,
  feedback: ClipApprovalFeedback | undefined
): boolean {
  const matching = matchingClipApprovalFeedback(clip, feedback);
  if (matching) return matching.status === 'approved';
  const review = record(clip.review);
  return record(review.approval).current === true;
}

/** Publish busy state immediately, then approval only after the request succeeds. */
export async function saveClipApproval(
  identity: string,
  save: () => Promise<unknown>,
  update: FeedbackUpdate
): Promise<ClipApprovalFeedback> {
  update({ status: 'saving', identity });
  try {
    await save();
    const approved: ClipApprovalFeedback = { status: 'approved', identity };
    update(approved);
    return approved;
  } catch (error) {
    const failed: ClipApprovalFeedback = {
      status: 'error',
      identity,
      message: error instanceof Error ? error.message : 'Could not approve clip',
    };
    update(failed);
    return failed;
  }
}

/** Drop local feedback once refreshed review data becomes authoritative. */
export function reconcileClipApprovalFeedback(
  feedback: ReadonlyMap<string, ClipApprovalFeedback>,
  clips: ReviewedClip[]
): ReadonlyMap<string, ClipApprovalFeedback> {
  if (feedback.size === 0) return feedback;
  const byId = new Map(
    clips.map((clip) => [String(clip.id ?? clip.clip_id), clip])
  );
  const next = new Map<string, ClipApprovalFeedback>();
  for (const [clipId, current] of feedback) {
    const clip = byId.get(clipId);
    if (!clip || current.identity !== clipApprovalIdentity(clip)) continue;
    const review = record(clip.review);
    const serverApproved = record(review.approval).current === true;
    if (!serverApproved) {
      next.set(clipId, current);
    }
  }
  if (
    next.size === feedback.size &&
    [...next].every(([clipId, value]) => feedback.get(clipId) === value)
  ) {
    return feedback;
  }
  return next;
}
