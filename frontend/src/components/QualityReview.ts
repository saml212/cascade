import { Button } from './Button';
import { h } from '../lib/dom';
import {
  api,
  type AudioRepairCandidate,
  type InspectionRequest,
  type OutputContinuityFinding,
  type OutputContinuityReport,
  type OutputFindingReviewEvent,
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
  inspectionPreviews?: Map<string, HTMLVideoElement>;
  reviewDrafts?: Map<string, { reviewer: string; evidenceNote: string }>;
  visibleFindingCount?: number;
  showResolvedFindings?: boolean;
}

export function QualityReview(options: QualityReviewOptions): HTMLElement {
  const { episodeId, quality, onUpdated, compact = false, controls } = options;
  const state = quality?.quality.status ?? 'missing';
  const revision = quality?.quality.current_revision ?? 'missing';
  const findings = quality?.audio_quality.findings ?? [];
  const blockers = quality?.release_gate.blockers ?? [];
  const audioGate = quality?.audio_quality.release_gate;
  const cameraSourceOnly =
    audioGate?.status === 'not_applicable' &&
    typeof audioGate.reason === 'string' &&
    audioGate.reason.includes('camera-source audio');
  const runButton = Button({
    variant: state === 'blocked' ? 'secondary' : 'primary',
    size: 'md',
    label: state === 'stale' ? 'Run QA for current revision' : 'Run quality review',
    onClick: async () => {
      const button = runButton as HTMLButtonElement;
      button.disabled = true;
      button.textContent = 'Running quality checks…';
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
          metric(
            cameraSourceOnly ? 'Camera-source findings' : 'Audio findings',
            cameraSourceOnly
              ? `${quality.audio_quality.finding_count} · reference only`
              : quality.audio_quality.finding_count
          )
        )
      : null,
    cameraSourceOnly && quality
      ? cameraSourceScope(quality.audio_quality.finding_count)
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
    !compact && quality?.audio_quality.selected_master_output_continuity
      ? outputContinuityReview(
          episodeId,
          quality.audio_quality.selected_master_output_continuity,
          controls,
          onUpdated
        )
      : null,
    !compact && findings.length
      ? findingList(
          episodeId,
          findings,
          revision,
          controls,
          onUpdated,
          cameraSourceOnly
        )
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
        ),
        selectedRepairBindingWarning(candidate, selection)
      ),
      action
    ),
    previewPlayer(
      'Repair draft · source clock',
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

function selectedRepairBindingWarning(
  candidate: AudioRepairCandidate,
  selection: QualitySnapshot['audio_quality']['repair_selection']
): HTMLElement | null {
  const bindingStatus = selection?.repair_binding_status;
  if (!bindingStatus || bindingStatus === 'current') return null;
  const staleCount = selection?.stale_repaired_finding_count;
  const staleDetail = staleCount == null
    ? 'One or more historical repair bindings no longer match current finding evidence.'
    : `${staleCount} historical repair ${staleCount === 1 ? 'binding is' : 'bindings are'} stale and ${staleCount === 1 ? 'is' : 'are'} not counted.`;
  return h(
    'p',
    { class: 'text-body-sm text-status-warning mt-2' },
    `Current QA projects ${candidate.repaired_finding_count} repaired findings. ${staleDetail}`
  );
}

function findingList(
  episodeId: string,
  findings: QualityFinding[],
  revision: string,
  controls?: QualityReviewControls,
  onUpdated?: () => void | Promise<void>,
  cameraSourceOnly = false
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
        cameraSourceOnly
          ? 'Camera-source findings · not used by selected mix'
          : 'Continuity findings requiring review'
      ),
      ...visible.map((finding) =>
        findingCard(episodeId, finding, revision, controls, onUpdated)
      ),
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
  episodeId: string,
  finding: QualityFinding,
  revision: string,
  controls?: QualityReviewControls,
  onUpdated?: () => void | Promise<void>
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
            'Camera reference (original)',
            preview.source,
            `${revision}:${finding.id}:source`,
            controls
          )
        : null,
      preview?.grounded_fallback
        ? previewPlayer(
            'Camera reference (surviving-channel patch)',
            preview.grounded_fallback,
            `${revision}:${finding.id}:grounded-fallback`,
            controls
          )
        : null,
      findingReviewAction(episodeId, finding, controls, onUpdated)
    )
  );
}


function findingReviewAction(
  episodeId: string,
  finding: QualityFinding,
  controls?: QualityReviewControls,
  onUpdated?: () => void | Promise<void>
): HTMLElement | null {
  const context = finding.review;
  if (!context) return null;
  return boundReviewAction({
    episodeId,
    identity: `finding-review:${context.report_fingerprint}:${context.finding_fingerprint}:${context.output_revision}`,
    allowed: context.allowed,
    reason: context.reason,
    outputRevision: context.output_revision,
    inspectionRequest: context.inspection_request,
    resolution: finding.resolution,
    heading: 'Review current rendered output',
    explanation:
      'This decision applies to this source finding and the exact current full render. It does not approve publishing or waive selected-master or rendered-output continuity failures.',
    acceptLabel: 'Accept issue for this render',
    falsePositiveLabel: 'Mark false positive for this render',
    successMessage: 'Finding review recorded for the current output.',
    controls,
    onUpdated,
    submit: (decision, reviewer, evidenceNote) =>
      api.reviewAudioFinding(episodeId, finding.id, {
        decision,
        reviewer,
        evidence_note: evidenceNote,
        expected_report_fingerprint: context.report_fingerprint,
        expected_finding_fingerprint: context.finding_fingerprint,
        expected_output_revision: context.output_revision!,
      }),
  });
}

interface BoundReviewOptions {
  episodeId: string;
  identity: string;
  allowed: boolean;
  reason?: string | null;
  outputRevision?: string | null;
  inspectionRequest?: InspectionRequest | null;
  resolution?: QualityFinding['resolution'];
  heading: string;
  explanation: string;
  acceptLabel: string;
  falsePositiveLabel: string;
  successMessage: string;
  controls?: QualityReviewControls;
  onUpdated?: () => void | Promise<void>;
  submit: (
    decision: 'accepted' | 'false_positive',
    reviewer: string,
    evidenceNote: string
  ) => Promise<unknown>;
}

function boundReviewAction(options: BoundReviewOptions): HTMLElement {
  const {
    episodeId,
    identity,
    allowed,
    reason,
    outputRevision,
    inspectionRequest,
    resolution,
    heading,
    explanation,
    acceptLabel,
    falsePositiveLabel,
    successMessage,
    controls,
    onUpdated,
    submit: recordDecision,
  } = options;
  if (!allowed || !outputRevision || !inspectionRequest) {
    return h(
      'div',
      {
        class:
          'rounded-md border border-border-subtle bg-surface-1 px-3 py-2 text-body-sm text-ink-tertiary',
      },
      reason ?? 'A current rendered-output review is unavailable.'
    );
  }
  const existingDecision = new Set(['accepted', 'false_positive']).has(
    resolution?.status ?? ''
  );
  const previewMap = inspectionPreviewMap(controls);
  const draftMap = reviewDraftMap(controls);
  const draft = draftMap.get(identity) ?? { reviewer: '', evidenceNote: '' };
  draftMap.set(identity, draft);
  const container = h('div', {
    class: 'rounded-md border border-accent/30 bg-accent/5 px-3 py-3 grid gap-3',
  });

  const render = (): void => {
    const video = previewMap.get(identity);
    const children: Node[] = [
      h(
        'div',
        { class: 'text-heading-sm uppercase text-ink-tertiary' },
        heading
      ),
      h(
        'p',
        { class: 'text-body-sm text-ink-secondary' },
        explanation
      ),
    ];
    if (existingDecision) {
      children.push(
        h(
          'p',
          { class: 'text-body-sm text-ink-secondary' },
          `Recorded as ${resolution?.status?.replace('_', ' ')} by ${resolution?.reviewed_by ?? 'reviewer'}${
            resolution?.evidence?.note ? `: ${resolution.evidence.note}` : '.'
          }`
        )
      );
    }
    if (!video) {
      const load = Button({
        variant: 'secondary',
        size: 'sm',
        label: 'Load exact full-output preview',
        onClick: async () => {
          load.disabled = true;
          load.textContent = 'Preparing bounded preview…';
          try {
            const result = await api.inspectionPreview(
              episodeId,
              inspectionRequest.query
            );
            previewMap.set(identity, inspectionPreviewVideo(result.asset.url));
            render();
          } catch (error) {
            showToast((error as Error).message, 'error');
            load.disabled = false;
            load.textContent = 'Load exact full-output preview';
          }
        },
      });
      children.push(load);
      container.replaceChildren(...children);
      return;
    }

    const reviewer = h('input', {
      type: 'text',
      value: draft.reviewer,
      placeholder: 'Reviewer name',
      class:
        'w-full rounded-md border border-border bg-surface-1 px-3 py-2 text-body text-ink-primary',
      oninput: (event: Event) => {
        draft.reviewer = (event.target as HTMLInputElement).value;
      },
    }) as HTMLInputElement;
    const note = h('textarea', {
      rows: 3,
      value: draft.evidenceNote,
      placeholder: 'What did you verify in this exact rendered passage?',
      class:
        'w-full rounded-md border border-border bg-surface-1 px-3 py-2 text-body text-ink-primary',
      oninput: (event: Event) => {
        draft.evidenceNote = (event.target as HTMLTextAreaElement).value;
      },
    }) as HTMLTextAreaElement;
    const actions: HTMLButtonElement[] = [];
    const submitDecision = async (
      decision: 'accepted' | 'false_positive'
    ): Promise<void> => {
      if (!draft.reviewer.trim() || !draft.evidenceNote.trim()) {
        showToast('Enter the reviewer and the evidence you verified.', 'error');
        return;
      }
      actions.forEach((button) => {
        button.disabled = true;
      });
      try {
        await recordDecision(
          decision,
          draft.reviewer.trim(),
          draft.evidenceNote.trim()
        );
        showToast(successMessage, 'success');
        await onUpdated?.();
      } catch (error) {
        showToast((error as Error).message, 'error');
        actions.forEach((button) => {
          button.disabled = false;
        });
      }
    };
    const accept = Button({
      variant: 'secondary',
      size: 'sm',
      label: acceptLabel,
      onClick: () => submitDecision('accepted'),
    });
    const falsePositive = Button({
      variant: 'secondary',
      size: 'sm',
      label: falsePositiveLabel,
      onClick: () => submitDecision('false_positive'),
    });
    actions.push(accept, falsePositive);
    children.push(
      h(
        'label',
        { class: 'text-body-sm text-ink-tertiary flex flex-col gap-1' },
        'Exact current longform output',
        video
      ),
      reviewer,
      note,
      h('div', { class: 'flex gap-2 flex-wrap' }, accept, falsePositive)
    );
    container.replaceChildren(...children);
  };
  render();
  return container;
}


function outputContinuityReview(
  episodeId: string,
  report: OutputContinuityReport,
  controls?: QualityReviewControls,
  onUpdated?: () => void | Promise<void>
): HTMLElement | null {
  const findings = report.findings ?? [];
  const reviewEvents = report.review_events ?? [];
  const groups = new Map<
    string,
    {
      role?: string;
      clipId?: string;
      status?: string;
      detail?: string;
      findings: OutputContinuityFinding[];
    }
  >();
  for (const artifact of report.artifacts ?? []) {
    const key = `${artifact.role ?? ''}\u0000${artifact.clip_id ?? ''}`;
    groups.set(key, {
      role: artifact.role,
      clipId: artifact.clip_id,
      status: artifact.status,
      detail: artifact.detail,
      findings: [],
    });
  }
  for (const finding of findings) {
    const key = `${finding.role ?? ''}\u0000${finding.clip_id ?? ''}`;
    const group = groups.get(key) ?? {
      role: finding.role,
      clipId: finding.clip_id,
      findings: [],
    };
    group.findings.push(finding);
    groups.set(key, group);
  }
  const visibleGroups = [...groups.values()].filter(
    (group) => group.status !== 'pass' || group.findings.length
  );
  if (
    (!report.status || report.status === 'pass') &&
    !visibleGroups.length &&
    !reviewEvents.length
  ) {
    return null;
  }
  const unresolvedReviewCount = reviewEvents.filter(
    (event) => !['accepted', 'false_positive'].includes(event.resolution?.status ?? '')
  ).length;
  const actionableReviewCount = reviewEvents.filter(
    (event) =>
      event.review.allowed &&
      !['accepted', 'false_positive'].includes(event.resolution?.status ?? '')
  ).length;
  return h(
    'div',
    { class: 'border-t border-border-subtle pt-4 flex flex-col gap-3' },
    h(
      'div',
      { class: 'text-heading-sm uppercase text-ink-tertiary' },
      'Selected-master and output continuity'
    ),
    h(
      'p',
      { class: 'text-body-sm text-ink-secondary' },
      report.current === false
        ? 'This output-continuity report is stale. Run QA before using its timestamps or previews.'
        : report.detail ?? 'Current output continuity evidence is incomplete.'
    ),
    ...(reviewEvents.length
      ? [
          h(
            'details',
            { class: 'rounded-md border border-accent/30 bg-accent/5 px-3 py-2' },
            h(
              'summary',
              { class: 'cursor-pointer text-body text-ink-primary font-medium' },
              actionableReviewCount
                ? `${actionableReviewCount} output audio ${actionableReviewCount === 1 ? 'event needs' : 'events need'} review`
                : unresolvedReviewCount
                  ? `${unresolvedReviewCount} semantic output ${unresolvedReviewCount === 1 ? 'prediction is' : 'predictions are'} unavailable for review`
                : `${reviewEvents.length} semantic output ${reviewEvents.length === 1 ? 'prediction reviewed' : 'predictions reviewed'}`
            ),
            h(
              'div',
              { class: 'mt-3 grid gap-3' },
              ...reviewEvents.map((event) =>
                outputReviewEvent(episodeId, event, controls, onUpdated)
              )
            )
          ),
        ]
      : []),
    ...visibleGroups.map((group) =>
      h(
        'details',
        {
          class:
            'rounded-md border border-status-danger/30 bg-status-danger/5 px-3 py-2 text-body-sm text-ink-secondary',
        },
        h(
          'summary',
          { class: 'cursor-pointer text-body text-ink-primary font-medium' },
          `${outputRole(group.role, group.clipId)} · ${group.status ?? 'finding'} · ${group.findings.length} artifact ${group.findings.length === 1 ? 'candidate' : 'candidates'}`
        ),
        h(
          'div',
          { class: 'mt-3 grid gap-3' },
          group.detail
            ? h('p', { class: 'text-body-sm text-ink-secondary' }, group.detail)
            : null,
          ...group.findings.map((finding) =>
            outputContinuityFinding(episodeId, finding, controls)
          )
        )
      )
    ),
    h(
      'p',
      { class: 'text-body-sm text-ink-tertiary' },
      'Per-artifact findings remain available above with their exact clocks and previews. Missing, stale, decode, and duration failures are hard checks; semantic decisions do not waive them, resolve source findings, or approve publishing.'
    )
  );
}

function outputReviewEvent(
  episodeId: string,
  event: OutputFindingReviewEvent,
  controls?: QualityReviewControls,
  onUpdated?: () => void | Promise<void>
): HTMLElement {
  const sourceRanges = event.binding.source_ranges
    .map((source) => timeRange(source.start_seconds, source.end_seconds))
    .join(', ');
  const context = event.review;
  const roles = [
    ...new Set(event.members.map((member) => outputRole(member.role, member.clip_id))),
  ].join(', ');
  const evidence = event.binding.transcript_evidence;
  const resolution = event.resolution?.status;
  const status = ['accepted', 'false_positive'].includes(resolution ?? '')
    ? ` · ${resolution?.replace('_', ' ')}`
    : '';
  return h(
    'details',
    { class: 'rounded-md border border-border-subtle bg-surface-2 px-4 py-3' },
    h(
      'summary',
      { class: 'cursor-pointer text-body text-ink-primary font-medium' },
      `Source ${sourceRanges || '—'} · ${event.members.length} exact ${event.members.length === 1 ? 'artifact' : 'artifacts'}${status}`
    ),
    h(
      'div',
      { class: 'mt-3 grid gap-3' },
      h('p', { class: 'text-body-sm text-ink-secondary' }, `Exact evidence match: ${roles}.`),
      h(
        'p',
        { class: 'text-body-sm text-ink-tertiary' },
        `Detector evidence: ${event.binding.classification.replaceAll('_', ' ')}; transcript timing overlaps ${evidence.speech_overlap_seconds.toFixed(3)}s across ${evidence.transcript_word_count} ${evidence.transcript_word_count === 1 ? 'word' : 'words'} (review threshold ${evidence.required_speech_overlap_seconds.toFixed(3)}s).`
      ),
      evidence.transcript_excerpt
        ? h(
            'blockquote',
            { class: 'text-body-sm text-ink-secondary border-l-2 border-border pl-3' },
            evidence.transcript_excerpt
          )
        : null,
      boundReviewAction({
        episodeId,
        identity: `output-review:${context.report_fingerprint}:${context.event_fingerprint}:${context.output_revision}`,
        allowed: context.allowed,
        reason: context.reason,
        outputRevision: context.output_revision,
        inspectionRequest: context.inspection_request,
        resolution: event.resolution,
        heading: 'Review semantic output prediction',
        explanation:
          'This records a judgment for these exact artifact revisions after reviewing the current full output. It does not alter word timing, waive missing, stale, decode, or duration failures, resolve source findings, or approve publishing.',
        acceptLabel: 'Accept as a real pause',
        falsePositiveLabel: 'Mark prediction false positive',
        successMessage: 'Output prediction review recorded for the current artifacts.',
        controls,
        onUpdated,
        submit: (decision, reviewer, evidenceNote) =>
          api.reviewAudioOutputFinding(episodeId, event.id, {
            decision,
            reviewer,
            evidence_note: evidenceNote,
            expected_report_fingerprint: context.report_fingerprint,
            expected_event_fingerprint: context.event_fingerprint,
            expected_output_revision: context.output_revision!,
          }),
      })
    )
  );
}

function outputContinuityFinding(
  episodeId: string,
  finding: OutputContinuityFinding,
  controls?: QualityReviewControls
): HTMLElement {
  const time = finding.artifact_time;
  const identity = `output-continuity:${finding.fingerprint ?? finding.id}`;
  return h(
    'details',
    { class: 'rounded-md border border-status-danger/30 bg-surface-2 px-4 py-3' },
    h(
      'summary',
      { class: 'cursor-pointer text-body text-ink-primary font-medium' },
      `${outputRole(finding.role, finding.clip_id)} · ${finding.kind ?? 'continuity failure'} · ${timeRange(time?.start_seconds, time?.end_seconds)} (${time?.clock ?? 'unknown'} clock)`
    ),
    h(
      'div',
      { class: 'mt-3 grid gap-3' },
      finding.evidence?.transcript_excerpt
        ? h(
            'blockquote',
            { class: 'text-body-sm text-ink-secondary border-l-2 border-border pl-3' },
            finding.evidence.transcript_excerpt
          )
        : null,
      finding.inspection_request
        ? lazyInspectionPreview(
            episodeId,
            finding.inspection_request,
            identity,
            'Inspect exact current output',
            controls
          )
        : h(
            'p',
            { class: 'text-body-sm text-ink-tertiary' },
            'No bounded canonical preview is available for this artifact. Repair or regenerate it, then rerun QA.'
          )
    )
  );
}


function lazyInspectionPreview(
  episodeId: string,
  request: InspectionRequest,
  identity: string,
  label: string,
  controls?: QualityReviewControls
): HTMLElement {
  const previewMap = inspectionPreviewMap(controls);
  const container = h('div', { class: 'grid gap-2' });
  const render = (): void => {
    const video = previewMap.get(identity);
    if (video) {
      container.replaceChildren(
        h(
          'label',
          { class: 'text-body-sm text-ink-tertiary flex flex-col gap-1' },
          label,
          video
        )
      );
      return;
    }
    const load = Button({
      variant: 'secondary',
      size: 'sm',
      label,
      onClick: async () => {
        load.disabled = true;
        try {
          const result = await api.inspectionPreview(episodeId, request.query);
          previewMap.set(identity, inspectionPreviewVideo(result.asset.url));
          render();
        } catch (error) {
          showToast((error as Error).message, 'error');
          load.disabled = false;
        }
      },
    });
    container.replaceChildren(load);
  };
  render();
  return container;
}


function inspectionPreviewVideo(src: string): HTMLVideoElement {
  return h('video', {
    controls: true,
    playsinline: true,
    preload: 'metadata',
    src,
    class: 'w-full rounded-md bg-black',
  }) as HTMLVideoElement;
}


function inspectionPreviewMap(
  controls?: QualityReviewControls
): Map<string, HTMLVideoElement> {
  if (!controls) return new Map();
  controls.inspectionPreviews ??= new Map();
  return controls.inspectionPreviews;
}


function reviewDraftMap(
  controls?: QualityReviewControls
): Map<string, { reviewer: string; evidenceNote: string }> {
  if (!controls) return new Map();
  controls.reviewDrafts ??= new Map();
  return controls.reviewDrafts;
}


function outputRole(role?: string, clipId?: string): string {
  if (role === 'selected_audio_master') return 'Selected audio master';
  if (role === 'upload_video') return 'Full upload video';
  if (role === 'podcast_audio') return 'Historical podcast MP3';
  if (role === 'short') return `Short${clipId ? ` ${clipId}` : ''}`;
  return role?.replaceAll('_', ' ') ?? 'Output artifact';
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

function cameraSourceScope(findingCount: number): HTMLElement {
  return h(
    'div',
    {
      class:
        'rounded-md bg-surface-2 border border-border-subtle px-4 py-3 text-body-sm text-ink-secondary',
      role: 'note',
    },
    `The continuity detector checked camera channels that the selected mix does not use. ${findingCount} ${
      findingCount === 1 ? 'finding remains' : 'findings remain'
    } as source evidence; this check is explicitly skipped in the release decision. Current output and release checks still apply.`
  );
}

function metric(label: string, value: number | string): HTMLElement {
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
