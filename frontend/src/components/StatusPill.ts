import { h } from '../lib/dom';
import { describeStatus, type StatusDescriptor, type StatusTone } from '../lib/format';

const TONE_CLASSES: Record<StatusTone, { dot: string; text: string; bg: string; border: string }> = {
  working: {
    dot: 'bg-accent shadow-[0_0_8px_rgba(245,165,36,0.55)] animate-pulse-breath',
    text: 'text-accent',
    bg: 'bg-accent/10',
    border: 'border-accent/30',
  },
  waiting: {
    dot: 'bg-status-warning',
    text: 'text-status-warning',
    bg: 'bg-status-warning/10',
    border: 'border-status-warning/25',
  },
  success: {
    dot: 'bg-status-success',
    text: 'text-status-success',
    bg: 'bg-status-success/10',
    border: 'border-status-success/25',
  },
  danger: {
    dot: 'bg-status-danger',
    text: 'text-status-danger',
    bg: 'bg-status-danger/10',
    border: 'border-status-danger/30',
  },
  neutral: {
    dot: 'bg-status-neutral',
    text: 'text-ink-secondary',
    bg: 'bg-surface-2',
    border: 'border-border',
  },
};

interface StatusPillOptions {
  raw?: string | null;
  descriptor?: StatusDescriptor;
  size?: 'sm' | 'md' | 'lg';
  showHint?: boolean;
}

export function StatusPill(opts: StatusPillOptions): HTMLElement {
  const d = opts.descriptor ?? describeStatus(opts.raw);
  const tone = TONE_CLASSES[d.tone];
  const sz = opts.size ?? 'md';
  const sizeClass =
    sz === 'lg'
      ? 'px-3 py-1.5 text-body gap-2'
      : sz === 'sm'
      ? 'px-2 py-0.5 text-body-sm gap-1.5'
      : 'px-2.5 py-1 text-body-sm gap-2';

  return h(
    'span',
    {
      class: `inline-flex items-center rounded-full border ${tone.bg} ${tone.border} ${tone.text} font-medium ${sizeClass}`,
      role: 'status',
      title: d.hint,
    },
    h('span', { class: `block rounded-full w-2 h-2 ${tone.dot}` }),
    h('span', null, d.label),
    opts.showHint && d.hint
      ? h('span', { class: 'text-ink-secondary font-normal ml-1' }, `— ${d.hint}`)
      : null
  );
}
