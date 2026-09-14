export type ClipReviewSurface = 'base' | 'background';

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
