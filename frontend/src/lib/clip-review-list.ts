interface ClipReviewCandidate {
  id?: unknown;
  clip_id?: unknown;
  review?: {
    selection?: { status?: string };
    render?: { current?: boolean };
  };
}

function activePriority(clip: ClipReviewCandidate): number {
  const selected = clip.review?.selection?.status === 'selected' ? 2 : 0;
  const current = clip.review?.render?.current ? 1 : 0;
  return selected + current;
}

export function groupClipReviewCandidates<T extends ClipReviewCandidate>(
  clips: readonly T[]
): { active: T[]; rejected: T[] } {
  const active: Array<{ clip: T; index: number }> = [];
  const rejected: T[] = [];

  clips.forEach((clip, index) => {
    if (clip.review?.selection?.status === 'rejected') {
      rejected.push(clip);
    } else {
      active.push({ clip, index });
    }
  });

  active.sort(
    (left, right) =>
      activePriority(right.clip) - activePriority(left.clip) ||
      left.index - right.index
  );

  return { active: active.map(({ clip }) => clip), rejected };
}

export function isRejectedClipId(
  clips: readonly ClipReviewCandidate[],
  clipId: string
): boolean {
  return clips.some(
    (clip) =>
      String(clip.id ?? clip.clip_id) === clipId &&
      clip.review?.selection?.status === 'rejected'
  );
}
