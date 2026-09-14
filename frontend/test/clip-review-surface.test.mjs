import assert from 'node:assert/strict';
import test from 'node:test';

import { importTs } from './load-ts.mjs';

const {
  ClipReviewSurfaceMemory,
  clipDistributionLabel,
  clipDistributionReady,
  clipDistributionSelectable,
  distributionChangeLockReason,
  distributionVersionLabel,
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

test('remembers each clip review surface across card reconstruction', () => {
  const memory = new ClipReviewSurfaceMemory();

  assert.equal(memory.get('clip_11', true), 'base');
  memory.select('clip_11', 'background');
  assert.equal(memory.get('clip_11', true), 'background');
  assert.equal(memory.get('clip_12', true), 'base');
  assert.equal(memory.get('clip_11', true), 'background');
});

test('falls back to base when a clip has no background surface', () => {
  const memory = new ClipReviewSurfaceMemory();

  memory.select('clip_11', 'background');
  assert.equal(memory.get('clip_11', false), 'base');
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
