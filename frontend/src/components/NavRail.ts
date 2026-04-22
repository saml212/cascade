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
    label: 'Dashboard',
    icon: () => Icon.dashboard(),
    matcher: (p) => p === '/' || p.startsWith('/episodes/'),
  },
  {
    path: '/new',
    label: 'New Episode',
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
                class: 'text-[10px] font-medium tracking-wide uppercase opacity-70',
              },
              item.label.split(' ')[0]
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
          class: 'flex items-center justify-center h-10 mb-2',
          title: 'Cascade',
        },
        h(
          'span',
          {
            class:
              'font-display text-display-md text-accent leading-none select-none',
          },
          'C'
        )
      ),
      list
    ),
    h(
      'div',
      { class: 'flex flex-col items-center gap-1 px-2' },
      h(
        'button',
        {
          class:
            'w-14 h-14 rounded-lg text-ink-tertiary hover:text-ink-primary hover:bg-surface-2 flex items-center justify-center',
          title: 'Settings',
        },
        Icon.settings()
      )
    )
  );
}
