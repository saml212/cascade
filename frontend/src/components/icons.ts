/**
 * Minimal icon set — 1.5px stroke, 20x20 viewBox. Monochromatic via
 * currentColor. Drawn from Lucide-style paths but inlined so we ship no
 * icon runtime.
 */

import { svg } from '../lib/dom';

interface IconProps {
  size?: number;
  class?: string;
  stroke?: number;
}

function icon(
  paths: string[],
  { size = 20, class: className = '', stroke = 1.5 }: IconProps = {}
): SVGElement {
  const el = svg(
    'svg',
    {
      viewBox: '0 0 20 20',
      fill: 'none',
      stroke: 'currentColor',
      'stroke-width': stroke,
      'stroke-linecap': 'round',
      'stroke-linejoin': 'round',
      width: size,
      height: size,
      class: className,
      'aria-hidden': 'true',
    }
  );
  for (const d of paths) {
    el.appendChild(
      svg('path', { d })
    );
  }
  return el;
}

export const Icon = {
  dashboard: (p?: IconProps) =>
    icon(['M3 3h6v8H3zM11 3h6v5h-6zM3 13h6v4H3zM11 10h6v7h-6z'], p),
  plus: (p?: IconProps) => icon(['M10 4v12M4 10h12'], p),
  calendar: (p?: IconProps) =>
    icon([
      'M3 5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z',
      'M3 8h14M7 2v3M13 2v3',
    ], p),
  chart: (p?: IconProps) =>
    icon(['M3 17V8M8 17V4M13 17v-7M17 17v-3'], p),
  chevronLeft: (p?: IconProps) => icon(['M12 4l-5 6 5 6'], p),
  chevronRight: (p?: IconProps) => icon(['M8 4l5 6-5 6'], p),
  chevronDown: (p?: IconProps) => icon(['M4 7l6 5 6-5'], p),
  play: (p?: IconProps) => icon(['M5 3v14l12-7z'], p),
  pause: (p?: IconProps) => icon(['M6 3v14M14 3v14'], p),
  film: (p?: IconProps) =>
    icon([
      'M3 3h14v14H3z',
      'M3 7h14M3 13h14M6 3v14M14 3v14',
    ], p),
};
