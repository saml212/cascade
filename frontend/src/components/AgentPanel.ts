import { h } from '../lib/dom';
import { effect, effectScope, onCleanup } from '../lib/signals';
import { agentPanelCollapsed, compactShell, toggleAgentPanel } from '../state/ui';
import { describeEpisodeStatus } from '../lib/format';
import { episodeDetail, episodeDetailId } from '../state/episodes';
import { Icon } from './icons';
import { EventFeed } from './EventFeed';

export function AgentPanel(): HTMLElement {
  const host = h('aside', {
    class:
      'shrink-0 bg-surface-canvas border-l border-border-subtle transition-[width] duration-[220ms] ease-expressive overflow-hidden relative z-10',
  });
  let viewDispose: (() => void) | null = null;

  effect(() => {
    const collapsed = agentPanelCollapsed();
    const compact = compactShell();
    host.classList.toggle('absolute', compact);
    host.classList.toggle('right-0', compact);
    host.classList.toggle('top-0', compact);
    host.classList.toggle('h-full', compact);
    host.classList.toggle('shadow-2xl', compact && !collapsed);
    host.style.width = collapsed ? '48px' : compact ? 'min(380px, calc(100vw - 64px))' : '380px';
    viewDispose?.();
    viewDispose = null;
    if (collapsed) {
      host.replaceChildren(collapsedView());
    } else {
      let view!: HTMLElement;
      viewDispose = effectScope(() => {
        view = expandedView();
      });
      host.replaceChildren(view);
    }
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
      { class: 'h-full w-full flex flex-col' },
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
              class: 'font-display text-heading-lg leading-none',
            },
            'Pipeline feed'
          ),
          h(
            'div',
            { class: 'text-body-sm text-ink-tertiary mt-1' },
            'What the cascade agent is doing'
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
        { class: 'flex-1 overflow-y-auto px-5 py-4 flex flex-col gap-4' },
        feedSection()
      )
    );
  }

  function feedSection(): HTMLElement {
    const currentState = h('div');
    const inner = h('div', null);
    let currentDispose: (() => void) | null = null;
    onCleanup(() => currentDispose?.());

    effect(() => {
      const id = episodeDetailId();
      const episode = episodeDetail();
      if (!id || !episode) {
        currentState.replaceChildren();
        return;
      }
      const status = describeEpisodeStatus(episode, {
        cropConfig: episode.crop_config,
        clips: episode.clips as unknown[] | undefined,
      });
      currentState.replaceChildren(
        h(
          'div',
          {
            class:
              'rounded-md bg-surface-2 border border-border-subtle px-3 py-2.5',
            role: 'status',
          },
          h(
            'div',
            { class: 'text-body text-ink-primary font-medium' },
            status.label
          ),
          h(
            'div',
            { class: 'text-body-sm text-ink-tertiary mt-0.5' },
            status.hint
          )
        )
      );
    });

    effect(() => {
      const id = episodeDetailId();

      // Synchronously cancel the previous feed's poll before replacing.
      if (currentDispose) {
        currentDispose();
        currentDispose = null;
      }

      if (!id) {
        inner.replaceChildren(
          h(
            'div',
            { class: 'text-body-sm text-ink-tertiary italic' },
            'Open an episode to see what the agent is up to.'
          )
        );
      } else {
        const { el, dispose } = EventFeed(id);
        currentDispose = dispose;
        inner.replaceChildren(el);
      }
    });
    return h('div', { class: 'flex flex-col gap-4' }, currentState, inner);
  }

  return host;
}
