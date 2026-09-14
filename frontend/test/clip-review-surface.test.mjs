import assert from 'node:assert/strict';
import test from 'node:test';

import { importTs } from './load-ts.mjs';

const {
  ClipReReleaseDraftStore,
  ClipReviewSurfaceMemory,
  clipCardStatusOverride,
  clipDistributionLabel,
  clipDistributionReady,
  clipDistributionSelectable,
  clipReReleaseViewState,
  confirmsClipReRelease,
  distributionChangeLockReason,
  distributionVersionLabel,
  publicationEvidenceStatusLabel,
  selectedDistributionVersion,
} = await importTs(
  new URL('../src/lib/clip-review-surface.ts', import.meta.url)
);

function reviewState({ distributionVersion = 'base', backgroundApproved = false } = {}) {
  const backgroundRevision = 'sha256:background-copy';
  const baseRevision = 'sha256:base-copy';
  return {
    render: { current: true, playable: true },
    approval: { status: 'current', current: true, revision: baseRevision },
    distribution: {
      version: distributionVersion,
      variant_id:
        distributionVersion === 'background_motion_v1'
          ? 'background_motion_v1'
          : null,
      label:
        distributionVersion === 'background_motion_v1'
          ? 'Motion background'
          : 'Base',
      current: true,
      approval_current: true,
      revision:
        distributionVersion === 'background_motion_v1'
          ? backgroundRevision
          : baseRevision,
      change_locked: false,
      change_lock_reason: null,
      re_release_allowed: false,
      re_release_reason: 'No prior remote submission requires a re-release.',
      re_release_request: null,
      re_release_request_consumed: null,
    },
    variants: {
      background_motion_v1: {
        id: 'background_motion_v1',
        label: 'Motion background',
        render: { current: true, playable: true },
        approval: {
          status: backgroundApproved ? 'current' : 'unapproved',
          current: backgroundApproved,
          revision: backgroundRevision,
        },
      },
    },
  };
}

test('preserves the chosen preview surface through next and previous navigation', () => {
  const memory = new ClipReviewSurfaceMemory();

  assert.equal(memory.get('clip_11', true), 'base');
  memory.select('clip_11', 'background');
  assert.equal(memory.get('clip_11', true), 'background');
  assert.equal(memory.get('clip_12', true), 'background');
  assert.equal(memory.get('clip_11', true), 'background');

  memory.select('clip_12', 'base');
  assert.equal(memory.get('clip_11', true), 'base');
});

test('temporarily falls back to base without losing background preference', () => {
  const memory = new ClipReviewSurfaceMemory();

  memory.select('clip_11', 'background');
  assert.equal(memory.get('clip_12', false), 'base');
  assert.equal(memory.get('clip_13', true), 'background');
});

test('uses the persisted exact version as the distribution identity', () => {
  const base = reviewState();
  assert.equal(clipDistributionReady(base), true);
  assert.equal(clipDistributionLabel(base), 'Base');
  assert.equal(selectedDistributionVersion(base).surface, 'base');

  const background = reviewState({
    distributionVersion: 'background_motion_v1',
    backgroundApproved: true,
  });
  assert.equal(clipDistributionReady(background), true);
  assert.equal(clipDistributionLabel(background), 'Motion background');
  assert.equal(selectedDistributionVersion(background).surface, 'background');
});

test('never substitutes a base approval for a background approval', () => {
  const review = reviewState({ distributionVersion: 'background_motion_v1' });

  assert.equal(review.approval.current, true);
  assert.equal(review.variants.background_motion_v1.approval.current, false);
  assert.equal(clipDistributionReady(review), false);
});

test('fails closed when distribution identity is malformed or incomplete', () => {
  const mismatched = reviewState();
  mismatched.distribution.variant_id = 'background_motion_v1';
  assert.equal(selectedDistributionVersion(mismatched), null);
  assert.equal(clipDistributionReady(mismatched), false);
  assert.equal(clipDistributionLabel(mismatched), 'Unknown version');

  const missingVariant = reviewState({
    distributionVersion: 'background_motion_v1',
    backgroundApproved: true,
  });
  delete missingVariant.variants.background_motion_v1;
  assert.equal(selectedDistributionVersion(missingVariant), null);
  assert.equal(clipDistributionReady(missingVariant), false);
});

test('shows the backend publication-history reason before changing versions', () => {
  const baseSelected = reviewState({ backgroundApproved: true });
  assert.equal(clipDistributionSelectable(baseSelected, 'background'), true);
  baseSelected.distribution.change_locked = true;
  baseSelected.distribution.re_release_allowed = true;
  baseSelected.distribution.change_lock_reason =
    'A scheduled Base receipt exists. Start an explicit re-release to change it.';

  assert.equal(
    distributionChangeLockReason(baseSelected),
    'A scheduled Base receipt exists. Start an explicit re-release to change it.'
  );
  assert.equal(clipDistributionSelectable(baseSelected, 'background'), false);

  const backgroundSelected = reviewState({
    distributionVersion: 'background_motion_v1',
    backgroundApproved: true,
  });
  assert.equal(clipDistributionSelectable(backgroundSelected, 'base'), true);
  backgroundSelected.distribution.change_locked = true;
  backgroundSelected.distribution.re_release_allowed = true;
  assert.equal(clipDistributionSelectable(backgroundSelected, 'base'), false);

  baseSelected.distribution.change_lock_reason = '   ';
  assert.match(
    distributionChangeLockReason(baseSelected),
    /Publication history locks/
  );

  baseSelected.distribution.change_locked = false;
  assert.equal(distributionChangeLockReason(baseSelected), null);

  delete baseSelected.distribution.change_locked;
  assert.match(distributionChangeLockReason(baseSelected), /status is unavailable/);
  assert.equal(clipDistributionSelectable(baseSelected, 'background'), false);
});

test('shows exact blocked re-release reason and validates prepared state', () => {
  const review = reviewState({
    distributionVersion: 'background_motion_v1',
    backgroundApproved: true,
  });
  review.distribution.change_locked = true;
  review.distribution.re_release_allowed = false;
  review.distribution.re_release_reason =
    'Receipt x has unresolved remote destinations. Refresh Upload-Post status.';

  assert.equal(
    distributionChangeLockReason(review),
    'Receipt x has unresolved remote destinations. Refresh Upload-Post status.'
  );
  assert.deepEqual(clipReReleaseViewState(review), {
    kind: 'blocked',
    reason:
      'Receipt x has unresolved remote destinations. Refresh Upload-Post status.',
  });

  review.distribution.re_release_allowed = true;
  review.distribution.re_release_reason = null;
  assert.deepEqual(clipReReleaseViewState(review), {
    kind: 'available',
    previousRequest: null,
  });

  review.distribution.re_release_request = {
    request_id: '323e4567-e89b-12d3-a456-426614174000',
    actor: 'Sam',
    reason: 'Updated short background',
    variant_id: 'background_motion_v1',
    target_revision: review.distribution.revision,
    render_fingerprint: 'sha256:render',
    receipt_history_revision: 'sha256:history',
    revision: 'sha256:request',
    created_at: '2026-09-13T20:00:00+00:00',
  };
  review.distribution.re_release_request_consumed = false;
  const prepared = clipReReleaseViewState(review);
  assert.equal(prepared.kind, 'prepared');
  assert.equal(prepared.request.request_id, review.distribution.re_release_request.request_id);

  review.distribution.re_release_request_consumed = true;
  review.distribution.re_release_allowed = false;
  review.distribution.re_release_reason =
    'Receipt next-request has unresolved remote destinations.';
  assert.deepEqual(clipReReleaseViewState(review), {
    kind: 'blocked',
    reason: 'Receipt next-request has unresolved remote destinations.',
  });

  review.distribution.re_release_allowed = true;
  const next = clipReReleaseViewState(review);
  assert.equal(next.kind, 'available');
  assert.equal(
    next.previousRequest.request_id,
    review.distribution.re_release_request.request_id
  );

  review.distribution.re_release_request = [];
  review.distribution.re_release_request_consumed = false;
  assert.equal(clipReReleaseViewState(review).kind, 'blocked');
});

test('fails closed when a prepared request no longer matches its target', () => {
  const review = reviewState({ backgroundApproved: true });
  review.distribution.change_locked = true;
  review.distribution.re_release_allowed = true;
  review.distribution.re_release_request_consumed = false;
  review.distribution.re_release_request = {
    request_id: '323e4567-e89b-12d3-a456-426614174000',
    actor: 'Sam',
    reason: 'Updated short background',
    variant_id: 'background_motion_v1',
    target_revision: 'sha256:stale-target',
    render_fingerprint: 'sha256:render',
    receipt_history_revision: 'sha256:history',
    revision: 'sha256:request',
    created_at: '2026-09-13T20:00:00+00:00',
  };

  assert.deepEqual(clipReReleaseViewState(review), {
    kind: 'blocked',
    reason:
      'The prepared re-release no longer matches the selected render and copy. Refresh before continuing.',
  });
});

test('persists one exact re-release request across retries and revisions', () => {
  const values = new Map();
  const storage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
  let nextId = 1;
  const target = {
    episodeId: 'episode',
    clipId: 'clip_11',
    variantId: 'background_motion_v1',
    expectedRevision: 'sha256:target-v1',
    previousRequestId: null,
  };
  const store = new ClipReReleaseDraftStore(
    storage,
    target,
    () => `323e4567-e89b-12d3-a456-42661417400${nextId++}`
  );
  const first = store.getOrCreate();
  const completed = { ...first, actor: 'Sam', reason: 'Use the new version' };
  store.save(completed);
  assert.deepEqual(store.getOrCreate(), completed);

  const nextRevision = new ClipReReleaseDraftStore(
    storage,
    { ...target, expectedRevision: 'sha256:target-v2' },
    () => `323e4567-e89b-12d3-a456-42661417400${nextId++}`
  ).getOrCreate();
  assert.notEqual(nextRevision.request_id, first.request_id);

  const nextRelease = new ClipReReleaseDraftStore(
    storage,
    { ...target, previousRequestId: first.request_id },
    () => `323e4567-e89b-12d3-a456-42661417400${nextId++}`
  ).getOrCreate();
  assert.notEqual(nextRelease.request_id, first.request_id);

  store.clear();
  assert.notEqual(store.getOrCreate().request_id, first.request_id);
});

test('clears a draft only for the exact confirmed re-release response', () => {
  const request = {
    variant_id: null,
    expected_revision: 'sha256:target',
    request_id: '323e4567-e89b-12d3-a456-426614174000',
    actor: 'Sam',
    reason: 'Prepare the reviewed base version',
  };
  const prepared = {
    status: 'prepared',
    clip_id: 'clip_11',
    requires_publish_approval: true,
    distribution: {
      variant_id: null,
      revision: request.expected_revision,
      re_release_request_consumed: false,
      re_release_request: {
        request_id: request.request_id,
        actor: request.actor,
        reason: request.reason,
        variant_id: request.variant_id,
        target_revision: request.expected_revision,
        render_fingerprint: 'sha256:render',
        receipt_history_revision: 'sha256:history',
        revision: 'sha256:request',
        created_at: '2026-09-13T20:00:00+00:00',
      },
    },
  };

  assert.equal(confirmsClipReRelease(prepared, 'clip_11', request), true);
  assert.equal(
    confirmsClipReRelease({ ...prepared, clip_id: 'clip_12' }, 'clip_11', request),
    false
  );
  assert.equal(
    confirmsClipReRelease(
      {
        ...prepared,
        distribution: {
          ...prepared.distribution,
          re_release_request_consumed: true,
        },
      },
      'clip_11',
      request
    ),
    false
  );
  assert.equal(
    confirmsClipReRelease(
      {
        ...prepared,
        status: 'already_prepared',
        distribution: {
          ...prepared.distribution,
          re_release_request_consumed: true,
        },
      },
      'clip_11',
      request
    ),
    true
  );
});

test('uses neutral approval and accurate partial publication language', () => {
  assert.deepEqual(clipCardStatusOverride('approved', true), {
    key: 'queued',
    tone: 'neutral',
    label: 'Approved',
    hint:
      'This clip is approved. Publication-history checks lock distribution changes; review its distribution details before a re-release.',
  });
  assert.equal(clipCardStatusOverride('pending', true), null);
  assert.equal(
    publicationEvidenceStatusLabel('partial_failure', false),
    'Some destinations failed'
  );
  assert.equal(publicationEvidenceStatusLabel('failed', false), 'Failed');
  assert.equal(publicationEvidenceStatusLabel('submitted', true), 'Scheduled');
});

test('labels schedule and receipt versions without hiding malformed identities', () => {
  assert.equal(distributionVersionLabel('base', null), 'Base');
  assert.equal(distributionVersionLabel(undefined, undefined), 'Base');
  assert.equal(
    distributionVersionLabel('background_motion_v1', 'background_motion_v1'),
    'Motion background'
  );
  assert.equal(
    distributionVersionLabel('base', 'background_motion_v1'),
    'Unknown version'
  );
});
