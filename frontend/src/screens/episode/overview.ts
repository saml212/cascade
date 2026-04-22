import { h } from '../../lib/dom';
import {
  CANONICAL_AGENTS,
  describeAgent,
  describeStatus,
  formatDuration,
  formatRelative,
  pluralize,
  summarizeErrorText,
} from '../../lib/format';
import { navigate } from '../../lib/router';

export function renderOverview(
  target: HTMLElement,
  ep: Record<string, unknown>,
  episodeId: string
): void {
  const status = describeStatus(ep.status as string, {
    cropConfig: ep.crop_config,
    clips: ep.clips as unknown[] | undefined,
  });
  const pipeline = ep.pipeline as Record<string, unknown> | undefined;
  const completed = (pipeline?.agents_completed as string[]) ?? [];
  const requested = (pipeline?.agents_requested as string[]) ?? [];
  const errors = (pipeline?.errors as Record<string, string>) ?? {};
  const clips = (ep.clips as unknown[]) ?? [];
  const tags = (ep.tags as string[]) ?? [];

  const description =
    (ep.episode_description as string) ||
    (ep.description as string) ||
    '';

  const cropConfigured = !!(ep.crop_config as unknown);
  const hasSync = !!(ep.audio_sync as unknown);

  target.replaceChildren(
    h(
      'div',
      { class: 'grid grid-cols-[2fr_1fr] gap-6' },
      // Main column
      h(
        'div',
        { class: 'flex flex-col gap-6' },
        // Status summary card
        h(
          'div',
          { class: 'panel p-6' },
          h(
            'div',
            { class: 'text-heading-sm uppercase text-ink-tertiary mb-2' },
            'Where we are'
          ),
          h(
            'div',
            { class: 'font-display text-display-md text-ink-primary' },
            status.hint || status.label
          ),
          pipeline?.current_agent
            ? h(
                'div',
                { class: 'text-body text-ink-secondary mt-3' },
                'Right now: ',
                h(
                  'span',
                  { class: 'text-ink-primary font-medium' },
                  describeAgent(pipeline.current_agent as string)
                )
              )
            : null
        ),

        // Full canonical 14-stage pipeline timeline
        h(
          'div',
          { class: 'panel p-6' },
          h(
            'h3',
            { class: 'text-heading-md text-ink-primary mb-4' },
            'Pipeline'
          ),
          agentTimeline(
            canonicalSequence(completed, requested),
            completed,
            pipeline?.current_agent as string | null,
            errors
          )
        ),

        // Description preview if present
        description
          ? h(
              'div',
              { class: 'panel p-6' },
              h(
                'h3',
                { class: 'text-heading-md text-ink-primary mb-3' },
                'Description'
              ),
              h(
                'p',
                { class: 'text-body-lg text-ink-secondary leading-relaxed whitespace-pre-line' },
                description.length > 500 ? description.slice(0, 500) + '…' : description
              )
            )
          : null
      ),

      // Side column
      h(
        'div',
        { class: 'flex flex-col gap-5' },
        h(
          'div',
          { class: 'panel p-5' },
          h(
            'div',
            { class: 'text-heading-sm uppercase text-ink-tertiary mb-3' },
            'Details'
          ),
          detailRow('Duration', formatDuration(ep.duration_seconds as number)),
          detailRow('Created', formatRelative(ep.created_at as string)),
          detailRow(
            'Speakers',
            cropConfigured ? speakerCountOf(ep) : 'not set'
          ),
          detailRow('Audio sync', hasSync ? 'Verified' : 'n/a'),
          detailRow('Clips', pluralize(clips.length, 'clip')),
          tags.length > 0 ? detailRow('Tags', `${tags.length}`) : null
        ),
        h(
          'div',
          { class: 'flex flex-col gap-2' },
          navigationRow(
            'Crop setup',
            'Adjust speaker crops + audio sync',
            () => navigate(`/episodes/${episodeId}/crop-setup`)
          ),
          navigationRow('Longform review', 'Watch the cut, request edits', () =>
            navigate(`/episodes/${episodeId}/longform/review`)
          ),
          navigationRow('Clip review', 'Keep/reject the 10 shorts, sign off per-platform', () =>
            navigate(`/episodes/${episodeId}/clips/review`)
          ),
          navigationRow('Publish', 'Schedule across platforms', () =>
            navigate(`/episodes/${episodeId}/publish`)
          )
        )
      )
    )
  );

}

function detailRow(label: string, value: string): HTMLElement {
  return h(
    'div',
    {
      class:
        'flex items-baseline justify-between gap-3 py-2 border-b border-border-subtle last:border-0',
    },
    h('span', { class: 'text-body-sm text-ink-tertiary' }, label),
    h('span', { class: 'text-body text-ink-primary font-mono tabular' }, value)
  );
}

function navigationRow(
  label: string,
  sub: string,
  onClick: () => void
): HTMLElement {
  return h(
    'button',
    {
      onclick: onClick,
      class:
        'panel text-left px-5 py-4 hover:border-border-strong hover:bg-surface-2 transition-colors duration-[120ms] flex items-center justify-between group',
    },
    h(
      'div',
      null,
      h('div', { class: 'text-body text-ink-primary font-medium' }, label),
      h('div', { class: 'text-body-sm text-ink-tertiary mt-0.5' }, sub)
    ),
    h(
      'span',
      {
        class:
          'text-ink-tertiary group-hover:text-accent transition-colors duration-[120ms]',
      },
      '→'
    )
  );
}

function renderAgentError(err: string): HTMLElement {
  return h(
    'div',
    {
      class:
        'mt-1 flex items-start gap-2 text-body-sm text-status-danger/90 leading-snug max-w-full',
      title: err,
    },
    h('span', { class: 'shrink-0 font-medium' }, 'Error:'),
    h(
      'span',
      { class: 'flex-1 min-w-0 truncate' },
      summarizeErrorText(err)
    )
  );
}

function speakerCountOf(ep: Record<string, unknown>): string {
  const cfg = ep.crop_config as Record<string, unknown> | undefined;
  if (!cfg) return 'not set';
  const speakers = cfg.speakers as unknown[] | undefined;
  if (speakers) return pluralize(speakers.length, 'speaker');
  if (cfg.speaker_l_center_x != null) return '2 speakers';
  return 'not set';
}

/**
 * Build the visible pipeline list: start with the canonical 14 stages,
 * then append any agents that actually ran but aren't in the canonical
 * set (legacy runs, renamed agents, etc.). Dedupes repeated entries so
 * re-runs of speaker_cut + transcribe + clip_miner collapse to one pill.
 */
function canonicalSequence(completed: string[], requested: string[]): string[] {
  const seen = new Set<string>();
  const seq: string[] = [];
  for (const a of CANONICAL_AGENTS) {
    if (!seen.has(a)) {
      seen.add(a);
      seq.push(a);
    }
  }
  for (const a of [...completed, ...requested]) {
    if (!seen.has(a)) {
      seen.add(a);
      seq.push(a);
    }
  }
  return seq;
}

function agentTimeline(
  agents: string[],
  completed: string[],
  current: string | null,
  errors: Record<string, string>
): HTMLElement {
  if (agents.length === 0) {
    return h(
      'p',
      { class: 'text-body-sm text-ink-tertiary italic' },
      'Pipeline has not started yet.'
    );
  }
  return h(
    'ol',
    { class: 'flex flex-col gap-0' },
    ...agents.map((a, i) => {
      const isDone = completed.includes(a);
      const isCurrent = current === a;
      const err = errors[a];
      const dotClass = err
        ? 'bg-status-danger'
        : isDone
        ? 'bg-status-success'
        : isCurrent
        ? 'bg-accent animate-pulse-breath'
        : 'bg-surface-3';
      return h(
        'li',
        {
          class: 'flex items-start gap-3 py-2.5',
        },
        h(
          'div',
          { class: 'flex flex-col items-center shrink-0' },
          h('span', { class: `w-2.5 h-2.5 rounded-full ${dotClass} mt-1` }),
          i < agents.length - 1
            ? h('span', {
                class: 'w-px flex-1 bg-border-subtle mt-1',
                style: { minHeight: '18px' },
              })
            : null
        ),
        h(
          'div',
          { class: 'flex-1 min-w-0' },
          h(
            'div',
            {
              class: [
                'text-body',
                err ? 'text-status-danger' : isCurrent ? 'text-ink-primary font-medium' : isDone ? 'text-ink-secondary' : 'text-ink-tertiary',
              ].join(' '),
            },
            describeAgent(a)
          ),
          err ? renderAgentError(err) : null
        )
      );
    })
  );
}
