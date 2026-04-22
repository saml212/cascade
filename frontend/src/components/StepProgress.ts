import { h } from '../lib/dom';

/**
 * 14-segment agent pipeline progress. Each segment is one of
 * done / current / errored / queued, tinted accordingly.
 */
interface StepProgressOptions {
  agents: string[];
  completed: string[];
  current: string | null;
  errored: string[];
}

export function StepProgress(opts: StepProgressOptions): HTMLElement {
  const items = opts.agents.map((a) => {
    const isDone = opts.completed.includes(a);
    const isCur = opts.current === a;
    const isErr = opts.errored.includes(a);
    const tone = isErr
      ? 'bg-status-danger/70'
      : isDone
      ? 'bg-status-success/70'
      : isCur
      ? 'bg-accent scanline-track'
      : 'bg-surface-2';
    return h('div', {
      class: `h-1.5 flex-1 rounded-full ${tone}`,
      title: a,
    });
  });
  return h('div', { class: 'flex gap-1 w-full' }, ...items);
}
