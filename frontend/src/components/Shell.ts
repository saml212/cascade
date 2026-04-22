import { h } from '../lib/dom';
import { effect } from '../lib/signals';
import { NavRail } from './NavRail';
import { AgentPanel } from './AgentPanel';
import { toast } from '../state/ui';

export function Shell(): { root: HTMLElement; main: HTMLElement } {
  const main = h('main', {
    class: 'flex-1 min-w-0 overflow-y-auto',
  });

  const toastHost = h('div', {
    class: 'fixed bottom-5 right-5 z-50 flex flex-col gap-2 pointer-events-none',
  });

  effect(() => {
    const t = toast();
    if (!t) {
      toastHost.replaceChildren();
      return;
    }
    const border =
      t.tone === 'error'
        ? 'border-status-danger/50'
        : t.tone === 'success'
        ? 'border-status-success/50'
        : 'border-border';
    toastHost.replaceChildren(
      h(
        'div',
        {
          class: `panel pointer-events-auto px-4 py-3 text-body ${border} animate-fade-up max-w-sm`,
        },
        t.message
      )
    );
  });

  const root = h(
    'div',
    { class: 'h-screen w-screen flex bg-canvas text-ink-primary' },
    NavRail(),
    main,
    AgentPanel(),
    toastHost
  );

  return { root, main };
}
