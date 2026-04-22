import { h, mount } from '../lib/dom';
import { link } from '../lib/router';

export function NotFound(target: HTMLElement): void {
  mount(
    target,
    h(
      'div',
      { class: 'max-w-[700px] mx-auto px-10 py-24 text-center' },
      h(
        'h1',
        { class: 'font-display text-display-xl text-ink-primary mb-4' },
        'Off the map.'
      ),
      h(
        'p',
        { class: 'text-body-lg text-ink-secondary mb-8' },
        'That route doesn’t exist. Back to the dashboard.'
      ),
      h(
        'a',
        {
          ...link('/'),
          class:
            'inline-flex items-center h-11 px-5 rounded-md bg-accent text-ink-on-accent font-medium',
        },
        'Dashboard →'
      )
    )
  );
}
