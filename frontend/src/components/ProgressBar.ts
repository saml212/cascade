import { h } from '../lib/dom';

export interface ProgressBarOptions {
  value?: number | null; // 0..1; null = indeterminate
  label?: string;
  detail?: string;
}

export function ProgressBar(opts: ProgressBarOptions): HTMLElement {
  const val = opts.value;
  const known = val != null && isFinite(val);
  const pct = known ? Math.max(0, Math.min(1, val)) * 100 : 0;

  const track = h(
    'div',
    {
      class:
        'relative h-1.5 rounded-full overflow-hidden ' +
        (known ? 'bg-surface-2' : 'scanline-track'),
    },
    known
      ? h('div', {
          class: 'h-full bg-accent transition-[width] duration-500 ease-expressive',
          style: { width: `${pct.toFixed(1)}%` },
        })
      : null
  );

  return h(
    'div',
    { class: 'flex flex-col gap-1.5' },
    opts.label || opts.detail
      ? h(
          'div',
          { class: 'flex items-baseline justify-between' },
          opts.label
            ? h('span', { class: 'text-body-sm text-ink-secondary' }, opts.label)
            : null,
          opts.detail
            ? h(
                'span',
                { class: 'text-code text-ink-tertiary font-mono tabular' },
                opts.detail
              )
            : null
        )
      : null,
    track
  );
}

/**
 * 14-segment agent pipeline progress.
 */
export interface StepProgressOptions {
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
