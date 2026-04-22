import { h, mount } from '../lib/dom';

export function Schedule(target: HTMLElement): void {
  mount(
    target,
    h(
      'div',
      { class: 'max-w-[1200px] mx-auto px-10 py-16' },
      h(
        'h1',
        { class: 'font-display text-display-xl text-ink-primary mb-3' },
        'Schedule.'
      ),
      h(
        'p',
        { class: 'text-body-lg text-ink-secondary max-w-2xl' },
        'Seven-day publishing calendar lands here — approved clips on platform-colored lanes, longforms pinned to their YouTube slot.'
      )
    )
  );
}
