import { h } from '../lib/dom';
import { effect, signal } from '../lib/signals';
import { subscribe, type PipelineEvent } from '../lib/events';

const KIND_STYLE: Record<PipelineEvent['kind'], { dot: string; text: string }> = {
  status: { dot: 'bg-ink-tertiary', text: 'text-ink-secondary' },
  agent_start: { dot: 'bg-accent animate-pulse-breath', text: 'text-ink-primary' },
  agent_done: { dot: 'bg-status-success', text: 'text-ink-secondary' },
  agent_error: { dot: 'bg-status-danger', text: 'text-status-danger' },
  tick: { dot: 'bg-ink-tertiary', text: 'text-ink-tertiary' },
  idle: { dot: 'bg-ink-tertiary', text: 'text-ink-tertiary' },
};

const MAX_EVENTS = 50;

export function EventFeed(episodeId: string): HTMLElement {
  const events = signal<PipelineEvent[]>([]);

  const unsubscribe = subscribe(episodeId, (e) => {
    events.set((prev) => [e, ...prev].slice(0, MAX_EVENTS));
  });

  const container = h('div', {
    class: 'flex flex-col gap-2',
    ref: (el: Element) => {
      // Clean up when removed from DOM.
      const observer = new MutationObserver(() => {
        if (!el.isConnected) {
          unsubscribe();
          observer.disconnect();
        }
      });
      observer.observe(document.body, { childList: true, subtree: true });
    },
  });

  const empty = h(
    'div',
    { class: 'text-body-sm text-ink-tertiary italic' },
    'No events yet.'
  );

  effect(() => {
    const items = events();
    if (items.length === 0) {
      container.replaceChildren(empty);
      return;
    }
    container.replaceChildren(
      ...items.map((e) => {
        const style = KIND_STYLE[e.kind];
        const time = new Date(e.at).toLocaleTimeString([], {
          hour: '2-digit',
          minute: '2-digit',
        });
        return h(
          'div',
          { class: 'flex gap-2.5 items-start text-body-sm' },
          h('span', {
            class: `mt-1.5 w-1.5 h-1.5 shrink-0 rounded-full ${style.dot}`,
          }),
          h(
            'div',
            { class: 'flex-1 min-w-0' },
            h(
              'div',
              { class: `${style.text} leading-snug` },
              e.label
            ),
            e.detail
              ? h(
                  'div',
                  { class: 'text-ink-tertiary text-body-sm mt-0.5' },
                  e.detail
                )
              : null
          ),
          h(
            'span',
            {
              class: 'text-code-sm text-ink-tertiary font-mono tabular shrink-0 mt-0.5',
            },
            time
          )
        );
      })
    );
  });

  return container;
}
