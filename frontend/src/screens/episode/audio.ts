import { h } from '../../lib/dom';
import { formatOffsetMs } from '../../lib/format';

export function renderAudio(
  target: HTMLElement,
  ep: Record<string, unknown>,
  _episodeId: string
): void {
  const sync = ep.audio_sync as Record<string, unknown> | undefined;
  const cropConfig = ep.crop_config as Record<string, unknown> | undefined;
  const speakers = (cropConfig?.speakers as Array<Record<string, unknown>>) ?? [];
  const ambient = (cropConfig?.ambient_tracks as Array<Record<string, unknown>>) ?? [];

  target.replaceChildren(
    h(
      'div',
      { class: 'flex flex-col gap-6' },
      sync
        ? h(
            'div',
            { class: 'panel p-6' },
            h('h3', { class: 'text-heading-md text-ink-primary mb-3' }, 'H6E sync'),
            h(
              'div',
              { class: 'grid grid-cols-3 gap-5 text-body' },
              syncStat('Offset', formatOffsetMs(sync.offset_seconds as number)),
              syncStat(
                'Confidence',
                sync.confidence != null
                  ? `${Math.round((sync.confidence as number) * 100)}%`
                  : '—'
              ),
              syncStat(
                'Drift',
                sync.drift_rate_ppm != null
                  ? `${(sync.drift_rate_ppm as number).toFixed(1)} ppm`
                  : '—'
              )
            )
          )
        : null,
      speakers.length > 0
        ? h(
            'div',
            { class: 'panel p-6' },
            h('h3', { class: 'text-heading-md text-ink-primary mb-3' }, 'Speakers'),
            ...speakers.map((s, i) =>
              h(
                'div',
                {
                  class:
                    'flex items-center justify-between py-2 border-b border-border-subtle last:border-0',
                },
                h(
                  'div',
                  { class: 'flex items-center gap-3' },
                  h('span', {
                    class: 'w-2.5 h-2.5 rounded-full',
                    style: { background: `var(--speaker-${((i % 4) + 1) as 1 | 2 | 3 | 4})` },
                  }),
                  h('span', { class: 'text-body text-ink-primary' }, (s.label as string) || `Speaker ${i + 1}`)
                ),
                h(
                  'span',
                  { class: 'text-code text-ink-tertiary font-mono tabular' },
                  s.track != null ? `Track ${s.track}` : '—'
                )
              )
            )
          )
        : null,
      ambient.length > 0
        ? h(
            'div',
            { class: 'panel p-6' },
            h('h3', { class: 'text-heading-md text-ink-primary mb-3' }, 'Ambient'),
            ...ambient.map((a) =>
              h(
                'div',
                {
                  class:
                    'flex items-center justify-between py-2 text-body text-ink-secondary',
                },
                h('span', null, (a.stem as string) || `Track ${a.track_number}`),
                h(
                  'span',
                  { class: 'text-code font-mono tabular' },
                  `vol ${((a.volume as number) ?? 0).toFixed(2)}`
                )
              )
            )
          )
        : null,
      !sync && speakers.length === 0
        ? h(
            'div',
            { class: 'panel p-10 text-center' },
            h(
              'div',
              {
                class:
                  'font-display text-display-md text-ink-secondary mb-2',
              },
              'Audio hasn’t been set up yet.'
            ),
            h(
              'p',
              { class: 'text-body text-ink-tertiary' },
              'Visit Crop setup to verify sync and assign speakers to tracks.'
            )
          )
        : null
    )
  );
}

function syncStat(label: string, value: string): HTMLElement {
  return h(
    'div',
    null,
    h('div', { class: 'text-heading-sm uppercase text-ink-tertiary mb-1' }, label),
    h('div', { class: 'text-body-lg text-ink-primary font-mono tabular' }, value)
  );
}
