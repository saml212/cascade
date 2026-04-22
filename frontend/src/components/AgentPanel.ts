import { h } from '../lib/dom';
import { effect } from '../lib/signals';
import { agentPanelCollapsed, toggleAgentPanel } from '../state/ui';
import { episodeDetailId } from '../state/episodes';
import { Icon } from './icons';
import { EventFeed } from './EventFeed';

export function AgentPanel(): HTMLElement {
  const host = h('aside', {
    class:
      'shrink-0 bg-surface-canvas border-l border-border-subtle transition-[width] duration-[220ms] ease-expressive overflow-hidden relative z-10',
  });

  effect(() => {
    const collapsed = agentPanelCollapsed();
    host.style.width = collapsed ? '48px' : '380px';
    host.replaceChildren(collapsed ? collapsedView() : expandedView());
  });

  function collapsedView(): HTMLElement {
    return h(
      'button',
      {
        onclick: toggleAgentPanel,
        class:
          'w-full h-full flex flex-col items-center justify-start gap-3 pt-5 text-ink-tertiary hover:text-ink-primary hover:bg-surface-1',
        title: 'Expand agent panel',
      },
      Icon.chevronLeft(),
      h(
        'span',
        {
          class:
            'font-display text-body text-ink-secondary [writing-mode:vertical-rl] [transform:rotate(180deg)] tracking-wide',
        },
        'Agent'
      )
    );
  }

  function expandedView(): HTMLElement {
    return h(
      'div',
      { class: 'h-full w-[380px] flex flex-col' },
      h(
        'div',
        {
          class:
            'flex items-center justify-between px-5 py-4 border-b border-border-subtle',
        },
        h(
          'div',
          null,
          h(
            'div',
            {
              class: 'font-display text-display-md leading-none',
            },
            'Agent'
          ),
          h(
            'div',
            { class: 'text-body-sm text-ink-tertiary mt-1' },
            'Pipeline activity'
          )
        ),
        h(
          'button',
          {
            onclick: toggleAgentPanel,
            class:
              'w-8 h-8 flex items-center justify-center rounded-md text-ink-tertiary hover:text-ink-primary hover:bg-surface-2',
            title: 'Collapse agent panel',
          },
          Icon.chevronRight()
        )
      ),
      h(
        'div',
        { class: 'flex-1 overflow-y-auto px-5 py-4 flex flex-col gap-5' },
        h(
          'div',
          {
            class: 'panel-inset px-4 py-3',
          },
          h(
            'div',
            { class: 'text-heading-sm uppercase text-ink-tertiary mb-1' },
            'Chat'
          ),
          h(
            'p',
            { class: 'text-body-sm text-ink-secondary leading-relaxed' },
            'Chat with the cascade agent arrives in the next phase. For now, the feed below streams what the agent is doing.'
          )
        ),
        feedSection()
      )
    );
  }

  function feedSection(): HTMLElement {
    const inner = h('div', null);
    effect(() => {
      const id = episodeDetailId();
      if (!id) {
        inner.replaceChildren(
          h(
            'div',
            { class: 'text-body-sm text-ink-tertiary italic' },
            'No episode selected.'
          )
        );
      } else {
        inner.replaceChildren(EventFeed(id));
      }
    });
    return h(
      'div',
      null,
      h(
        'div',
        { class: 'text-heading-sm uppercase text-ink-tertiary mb-3' },
        'Live feed'
      ),
      inner
    );
  }

  return host;
}
