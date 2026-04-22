import { h } from '../lib/dom';

type Variant = 'primary' | 'secondary' | 'ghost' | 'destructive';
type Size = 'sm' | 'md' | 'lg';

interface ButtonOptions {
  variant?: Variant;
  size?: Size;
  label: string;
  icon?: SVGElement | HTMLElement;
  iconRight?: SVGElement | HTMLElement;
  onClick?: (e: MouseEvent) => void;
  disabled?: boolean;
  loading?: boolean;
  title?: string;
  type?: 'button' | 'submit';
  class?: string;
}

const VARIANT: Record<Variant, string> = {
  primary:
    'bg-accent text-ink-on-accent hover:brightness-110 active:brightness-95 border-transparent',
  secondary:
    'bg-surface-2 text-ink-primary hover:bg-surface-3 border-border',
  ghost:
    'bg-transparent text-ink-secondary hover:text-ink-primary hover:bg-surface-2 border-transparent',
  destructive:
    'bg-transparent text-status-danger hover:bg-status-danger/10 border-status-danger/40',
};

const SIZE: Record<Size, string> = {
  sm: 'h-7 px-2.5 text-body-sm gap-1.5',
  md: 'h-9 px-3.5 text-body gap-2',
  lg: 'h-11 px-5 text-body-lg gap-2.5',
};

export function Button(opts: ButtonOptions): HTMLButtonElement {
  const variant = opts.variant ?? 'secondary';
  const size = opts.size ?? 'md';
  const classes = [
    'inline-flex items-center justify-center rounded-md border font-medium',
    'transition-colors duration-[120ms] ease-expressive',
    'disabled:opacity-40 disabled:cursor-not-allowed',
    VARIANT[variant],
    SIZE[size],
    opts.class ?? '',
  ].join(' ');

  const btn = h(
    'button',
    {
      type: opts.type ?? 'button',
      class: classes,
      disabled: opts.disabled || opts.loading,
      title: opts.title,
      onclick: opts.onClick,
    },
    opts.loading
      ? h('span', {
          class:
            'inline-block w-3 h-3 rounded-full border border-current border-t-transparent animate-spin',
        })
      : opts.icon ?? null,
    h('span', null, opts.label),
    opts.iconRight ?? null
  ) as HTMLButtonElement;

  return btn;
}
