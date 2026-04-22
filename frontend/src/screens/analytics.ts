import { h, mount } from '../lib/dom';

export function Analytics(target: HTMLElement): void {
  mount(
    target,
    h(
      'div',
      { class: 'max-w-[1200px] mx-auto px-10 py-24 text-center' },
      h(
        'h1',
        { class: 'font-display text-display-xl text-ink-primary mb-4' },
        'Too early to tell.'
      ),
      h(
        'p',
        { class: 'text-body-lg text-ink-secondary max-w-xl mx-auto' },
        'Performance data lands a week after each episode publishes. Nothing to show yet.'
      )
    )
  );
}
