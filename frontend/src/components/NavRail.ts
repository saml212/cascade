import { h } from '../lib/dom';
import { effect } from '../lib/signals';
import { currentPath, link } from '../lib/router';
import { Icon } from './icons';

interface NavItem {
  path: string;
  label: string;
  icon: () => SVGElement;
  matcher: (path: string) => boolean;
}

const ITEMS: NavItem[] = [
  {
    path: '/',
    label: 'Episodes',
    icon: () => Icon.dashboard(),
    matcher: (p) => p === '/' || p.startsWith('/episodes/'),
  },
  {
    path: '/new',
    label: 'New',
    icon: () => Icon.plus(),
    matcher: (p) => p === '/new',
  },
  {
    path: '/schedule',
    label: 'Schedule',
    icon: () => Icon.calendar(),
    matcher: (p) => p === '/schedule',
  },
  {
    path: '/analytics',
    label: 'Analytics',
    icon: () => Icon.chart(),
    matcher: (p) => p === '/analytics',
  },
];

export function NavRail(): HTMLElement {
  const list = h('ul', { class: 'flex flex-col gap-1 px-2 mt-4' });

  effect(() => {
    const path = currentPath();
    list.replaceChildren(
      ...ITEMS.map((item) => {
        const active = item.matcher(path);
        return h(
          'li',
          null,
          h(
            'a',
            {
              ...link(item.path),
              class: [
                'group relative flex flex-col items-center justify-center gap-1',
                'w-14 h-14 mx-auto rounded-lg transition-colors duration-[120ms]',
                active
                  ? 'text-ink-primary bg-surface-2'
                  : 'text-ink-tertiary hover:text-ink-primary hover:bg-surface-2',
              ].join(' '),
              title: item.label,
            },
            active
              ? h('span', {
                  class: 'absolute left-0 top-2.5 bottom-2.5 w-[2px] rounded-r bg-accent',
                })
              : null,
            item.icon(),
            h(
              'span',
              {
                class: 'text-[9.5px] font-medium tracking-wide uppercase opacity-80',
              },
              item.label
            )
          )
        );
      })
    );
  });

  return h(
    'aside',
    {
      class:
        'w-[72px] shrink-0 bg-surface-canvas border-r border-border-subtle flex flex-col justify-between py-4',
    },
    h(
      'div',
      null,
      h(
        'a',
        {
          ...link('/'),
          class:
            'relative flex items-center justify-center h-10 w-10 mx-auto mb-3 rounded-[10px] bg-gradient-to-b from-accent to-[#c97f12] text-ink-on-accent shadow-[0_4px_14px_rgba(245,165,36,0.35),inset_0_1px_0_rgba(255,255,255,0.25)] hover:brightness-110 transition-[filter] duration-[120ms]',
          title: 'Cascade',
        },
        h(
          'span',
          {
            class:
              'font-display font-semibold text-[20px] leading-none select-none tracking-tight',
          },
          'C'
        )
      ),
      list
    ),
    h(
      'div',
      {
        class:
          'flex flex-col items-center gap-1 px-2 text-code-sm text-ink-disabled font-mono tabular select-none',
        title: 'Cascade frontend version',
      },
      h('span', null, 'v0.1')
    )
  );
}
