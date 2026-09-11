import { Button } from './Button';
import { h } from '../lib/dom';
import {
  api,
  type QualityFinding,
  type QualitySnapshot,
} from '../lib/api';
import { showToast } from '../state/ui';

interface QualityReviewOptions {
  episodeId: string;
  quality?: QualitySnapshot | null;
  onUpdated?: () => void | Promise<void>;
  compact?: boolean;
  controls?: QualityReviewControls;
}

export interface QualityReviewControls {
  previews: Map<string, HTMLAudioElement>;
  visibleFindingCount?: number;
  showResolvedFindings?: boolean;
}

export function QualityReview(options: QualityReviewOptions): HTMLElement {
  const { episodeId, quality, onUpdated, compact = false, controls } = options;
  const state = quality?.quality.status ?? 'missing';
  const revision = quality?.quality.current_revision ?? 'missing';
  const findings = quality?.audio_quality.findings ?? [];
  const blockers = quality?.release_gate.blockers ?? [];
  const runButton = Button({
    variant: state === 'blocked' ? 'secondary' : 'primary',
    size: 'md',
    label: state === 'stale' ? 'Run QA for current revision' : 'Run quality review',
    onClick: async () => {
      const button = runButton as HTMLButtonElement;
      button.disabled = true;
      button.textContent = 'Analyzing source audio…';
      try {
        await api.runQuality(episodeId);
        showToast('Quality report updated.', 'success');
        await onUpdated?.();
      } catch (error) {
        showToast((error as Error).message, 'error');
        await onUpdated?.();
      } finally {
        button.disabled = false;
        button.textContent = 'Run quality review';
      }
    },
  });

  return h(
    'section',
    { class: 'panel p-6 flex flex-col gap-4' },
    h(
      'div',
      { class: 'flex items-start justify-between gap-5 flex-wrap' },
      h(
        'div',
        null,
        h('div', { class: 'text-heading-sm uppercase text-ink-tertiary' }, 'Quality review'),
        h(
          'div',
          { class: `font-display text-display-md mt-1 ${stateTone(state)}` },
          stateLabel(state)
        ),
        h(
          'p',
          { class: 'text-body-sm text-ink-secondary mt-2 max-w-[620px]' },
          stateDetail(quality)
        )
      ),
      runButton
    ),
    blockers.length
      ? h(
          'div',
          { class: 'rounded-md bg-status-warning/10 border border-status-warning/30 px-4 py-3' },
          ...blockers.map((blocker) =>
            h(
              'div',
              { class: 'text-body-sm text-ink-secondary py-1' },
              blocker.message
            )
          )
        )
      : null,
    quality
      ? h(
          'div',
          { class: 'grid grid-cols-2 sm:grid-cols-4 gap-3' },
          metric('Candidates', quality.artifacts.candidate_count),
          metric('Shorts rendered', quality.artifacts.rendered_short_count),
          metric('Awaiting clip review', quality.artifacts.pending_clip_count),
          metric('Audio findings', quality.audio_quality.finding_count)
        )
      : null,
    !compact && quality?.audio_quality.repair_candidate
      ? repairCandidate(
          episodeId,
          quality.audio_quality.repair_candidate,
          quality.audio_quality.repair_selection,
          controls,
          onUpdated
        )
      : null,
    !compact && findings.length
      ? findingList(findings, revision, controls)
      : null
  );
}

function repairCandidate(
  episodeId: string,
  candidate: NonNullable<QualitySnapshot['audio_quality']['repair_candidate']>,
  selection: QualitySnapshot['audio_quality']['repair_selection'],
  controls?: QualityReviewControls,
  onUpdated?: () => void | Promise<void>
): HTMLElement {
  const selected = Boolean(selection && selection.status !== 'not_selected');
  const selectable = candidate.current && candidate.verification_status === 'pass';
  const action = Button({
    variant: selected ? 'secondary' : 'primary',
    size: 'sm',
    disabled: !selected && !selectable,
    label: selected ? 'Stop using repair draft' : 'Use draft for future renders',
    onClick: async () => {
      action.disabled = true;
      try {
        if (selected) {
          await api.clearAudioRepairSelection(episodeId);
          showToast('Repair draft selection cleared.', 'success');
        } else {
          await api.selectAudioRepairCandidate(episodeId);
          showToast(
            'Repair draft selected. Run quality review to bind the selected output.',
            'success'
          );
        }
        await onUpdated?.();
      } catch (error) {
        showToast((error as Error).message, 'error');
      } finally {
        action.disabled = false;
      }
    },
  });
  const status = selection?.status === 'stale'
    ? `Selected repair is stale: ${selection.detail ?? 'inputs changed'}`
    : selected
      ? 'This repair draft is the audio source for future renders.'
      : selectable
        ? 'Objective signal checks passed; the draft still requires media review.'
        : 'Candidate evidence is stale or incomplete. Rebuild it before selection.';

  return h(
    'div',
    { class: 'rounded-md border border-border-subtle bg-surface-2 px-4 py-4 grid gap-3' },
    h(
      'div',
      { class: 'flex items-start justify-between gap-4 flex-wrap' },
      h(
        'div',
        null,
        h('div', { class: 'text-heading-sm uppercase text-ink-tertiary' }, 'Grounded audio repair draft'),
        h('p', { class: 'text-body-sm text-ink-secondary mt-1' }, status),
        h(
          'p',
          { class: 'text-body-sm text-ink-tertiary mt-1' },
          `${candidate.repaired_finding_count} findings repaired · ${candidate.unresolved_finding_count} unresolved · human listening ${candidate.perceptual_review?.status === 'not_performed' ? 'not performed' : 'not recorded'}`
        )
      ),
      action
    ),
    previewPlayer(
      'Full repair draft',
      candidate.audio_url,
      `repair-candidate:${candidate.fingerprint ?? 'unknown'}`,
      controls
    ),
    h(
      'p',
      { class: 'text-body-sm text-ink-tertiary' },
      'Selecting this draft changes future render input. It does not approve publishing or mark unresolved findings safe.'
    )
  );
}

function findingList(
  findings: QualityFinding[],
  revision: string,
  controls?: QualityReviewControls
): HTMLElement {
  const panel = h('div', {
    class: 'border-t border-border-subtle pt-4 flex flex-col gap-3',
  });
  const needsReview = findings.filter(needsFindingReview);
  const resolved = findings.filter((finding) => !needsFindingReview(finding));
  let visibleCount = controls?.visibleFindingCount ?? 10;
  let showResolved = controls?.showResolvedFindings ?? false;

  const render = (): void => {
    const available = showResolved ? [...needsReview, ...resolved] : needsReview;
    const visible = available.slice(0, visibleCount);
    const children: Node[] = [
      h(
        'div',
        { class: 'text-heading-sm uppercase text-ink-tertiary' },
        'Continuity findings requiring review'
      ),
      ...visible.map((finding) => findingCard(finding, revision, controls)),
    ];
    if (available.length > visible.length) {
      children.push(
        Button({
          variant: 'secondary',
          size: 'sm',
          label: `Show 10 more (${available.length - visible.length} remaining)`,
          onClick: () => {
            visibleCount += 10;
            if (controls) controls.visibleFindingCount = visibleCount;
            render();
          },
        })
      );
    }
    if (resolved.length && !showResolved) {
      children.push(
        Button({
          variant: 'ghost',
          size: 'sm',
          label: `Show ${resolved.length} removed or resolved finding(s)`,
          onClick: () => {
            showResolved = true;
            if (controls) controls.showResolvedFindings = true;
            render();
          },
        })
      );
    }
    panel.replaceChildren(...children);
  };
  render();
  return panel;
}

function needsFindingReview(finding: QualityFinding): boolean {
  const resolution = finding.resolution as Record<string, unknown> | undefined;
  const resolved = new Set([
    'repaired',
    'accepted',
    'false_positive',
    'not_in_selected_mix',
  ]);
  return (
    finding.edited_time?.status !== 'removed' &&
    !resolved.has(String(resolution?.status ?? 'unresolved'))
  );
}

function findingCard(
  finding: QualityFinding,
  revision: string,
  controls?: QualityReviewControls
): HTMLElement {
  const source = finding.source_time;
  const edited = finding.edited_time;
  const preview = finding.preview;
  const excerpt = finding.evidence?.transcript_excerpt;
  const label = findingLabel(finding);
  const sourceRange = timeRange(source?.start_seconds, source?.end_seconds);
  const outputRange = editedRange(edited);
  return h(
    'details',
    { class: 'rounded-md border border-border-subtle bg-surface-2 px-4 py-3' },
    h(
      'summary',
      { class: 'cursor-pointer flex items-center justify-between gap-4' },
      h(
        'span',
        { class: 'text-body text-ink-primary font-medium' },
        `${label} · source ${sourceRange} · output ${outputRange} · channel ${
          typeof finding.channel === 'number' ? finding.channel + 1 : '—'
        }`
      ),
      h(
        'span',
        { class: `text-body-sm ${finding.severity === 'error' ? 'text-status-danger' : 'text-status-warning'}` },
        finding.confidence == null
          ? finding.severity
          : `${finding.severity} · heuristic score ${(finding.confidence * 100).toFixed(0)}%`
      )
    ),
    h(
      'div',
      { class: 'mt-3 grid gap-3' },
      h(
        'div',
        { class: 'text-body-sm text-ink-secondary' },
        `Source clock ${sourceRange} · edited output ${outputRange}`
      ),
      typeof excerpt === 'string' && excerpt
        ? h('blockquote', { class: 'text-body-sm text-ink-secondary border-l-2 border-border pl-3' }, excerpt)
        : null,
      preview?.source
        ? previewPlayer(
            'Original source mix',
            preview.source,
            `${revision}:${finding.id}:source`,
            controls
          )
        : null,
      preview?.grounded_fallback
        ? previewPlayer(
            'Grounded surviving-channel preview',
            preview.grounded_fallback,
            `${revision}:${finding.id}:grounded-fallback`,
            controls
          )
        : null
    )
  );
}

function findingLabel(finding: QualityFinding): string {
  if (finding.classification === 'probable_dropout') return 'Probable audio dropout';
  if (finding.classification === 'candidate_discontinuity') {
    return 'Possible audio discontinuity';
  }
  if (finding.kind === 'digital_zero') return 'Digital silence';
  if (finding.kind === 'sharp_level_collapse') return 'Sharp level drop';
  return 'Audio continuity finding';
}

function previewPlayer(
  label: string,
  src: string,
  identity: string,
  controls?: QualityReviewControls
): HTMLElement {
  let audio = controls?.previews.get(identity);
  if (!audio) {
    audio = h('audio', {
      controls: true,
      preload: 'none',
      src,
      class: 'w-full',
    }) as HTMLAudioElement;
    controls?.previews.set(identity, audio);
  }
  return h(
    'label',
    { class: 'text-body-sm text-ink-tertiary flex flex-col gap-1' },
    label,
    audio
  );
}

function metric(label: string, value: number): HTMLElement {
  return h(
    'div',
    { class: 'rounded-md bg-surface-2 px-3 py-2' },
    h('div', { class: 'text-heading-sm uppercase text-ink-tertiary' }, label),
    h('div', { class: 'font-mono tabular text-body-lg text-ink-primary mt-1' }, String(value))
  );
}

function stateLabel(state: QualitySnapshot['quality']['status']): string {
  if (state === 'passed') return 'Reviewed for this revision';
  if (state === 'blocked') return 'Release blocked';
  if (state === 'stale') return 'Review is out of date';
  return 'Quality review required';
}

function stateTone(state: QualitySnapshot['quality']['status']): string {
  if (state === 'passed') return 'text-status-success';
  if (state === 'blocked') return 'text-status-danger';
  return 'text-status-warning';
}

function stateDetail(quality?: QualitySnapshot | null): string {
  if (!quality) return 'No revisioned QA report exists yet.';
  if (quality.quality.status === 'passed') {
    return 'The listed automated checks passed. Review the report limitations and media before publishing.';
  }
  if (quality.quality.status === 'stale') {
    return 'Source, edits, rendered media, or release copy changed after QA.';
  }
  if (quality.quality.status === 'blocked') {
    return 'The current report found unresolved release issues. Existing files remain available for review.';
  }
  return 'Run QA to inspect source-channel continuity and current release artifacts.';
}

function timeRange(start?: number, end?: number): string {
  if (start == null || end == null) return 'unavailable';
  return `${timecode(start)}–${timecode(end)}`;
}

function editedRange(edited?: QualityFinding['edited_time']): string {
  if (!edited || edited.status === 'unavailable') return 'unavailable';
  if (edited.status === 'removed') return 'removed by edit';
  return edited.ranges
    .map((range) => timeRange(range.start_seconds, range.end_seconds))
    .join(', ');
}

function timecode(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds - minutes * 60;
  return `${String(minutes).padStart(2, '0')}:${remainder.toFixed(2).padStart(5, '0')}`;
}
